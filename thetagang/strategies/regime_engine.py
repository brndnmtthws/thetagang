from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Coroutine, Dict, Iterable, List, Optional, Tuple

import exchange_calendars as xcals
import numpy as np
import pandas as pd
from ib_async import AccountValue, ExecutionFilter, PortfolioItem, Ticker
from ib_async.contract import Option, Stock
from rich.table import Table

from thetagang import log
from thetagang.config import Config
from thetagang.config_models import RegimeRebalanceBaseEnum
from thetagang.db import DataStore
from thetagang.fmt import dfmt, ffmt, ifmt, pfmt
from thetagang.ibkr import IBKR, TickerField
from thetagang.orders import PendingBuyCash, pending_buy_cash
from thetagang.strategies.runtime_services import resolve_symbol_configs
from thetagang.strategies.tail_hedge_state import (
    TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX,
    TailHedgeCohort,
    TailHedgeStateStore,
    build_tail_reduction_order_ref,
    is_tail_reduction_ref,
    parse_state_datetime,
)
from thetagang.trading_operations import OrderOperations
from thetagang.util import midpoint_or_market_price, portfolio_positions_to_dict

AlignedClosesResult = Tuple[List[date], Dict[str, List[float]]]
AlignedClosesFetcher = Callable[
    [List[str], int, int], Coroutine[Any, Any, AlignedClosesResult]
]
ClosesBySymbol = Dict[str, Dict[date, float]]
TRADING_DAYS_PER_YEAR = 252
REGIME_HISTORY_TIMEFRAME = "1 day"
REGIME_HISTORY_MAX_ATTEMPTS = 3
REGIME_HISTORY_RETRY_DELAY_SECONDS = 0.25
TAIL_HEDGE_HARVEST_EVENT = "tail_hedge_harvest"
TAIL_HEDGE_HARVEST_SCHEMA_VERSION = 1


class RegimeHistoryValidationError(ValueError):
    def __init__(self, message: str, *, cache_recoverable: bool) -> None:
        super().__init__(message)
        self.cache_recoverable = cache_recoverable


@dataclass(frozen=True)
class RatioGateResult:
    ok: bool
    reason: str
    anchor: str
    rest: List[str]
    weights: Dict[str, float]
    daily_mean: Optional[float]
    daily_std: Optional[float]
    daily_var: Optional[float]
    annualized_vol: Optional[float]
    vol_min: float
    tstat: float
    drift_max: float

    def to_payload(self, *, enabled: bool) -> Dict[str, Any]:
        return {
            "enabled": enabled,
            "anchor": self.anchor,
            "rest": self.rest,
            "weights": self.weights,
            "var": self.daily_var,
            "vol": self.annualized_vol,
            "vol_min": self.vol_min,
            "std_daily": self.daily_std,
            "mean_daily": self.daily_mean,
            "tstat": self.tstat,
            "drift_max": self.drift_max,
            "reason": self.reason,
            "ok": self.ok,
        }

    def to_log_fields(self) -> str:
        return (
            f" ratio_ok={self.ok} "
            f"ratio_reason={self.reason} "
            f"ratio_var={_ffmt_or_dash(self.daily_var, 8)} "
            f"ratio_vol={_pfmt_or_dash(self.annualized_vol)} "
            f"ratio_vol_min={_pfmt_or_dash(self.vol_min)} "
            f"ratio_tstat={_ffmt_or_dash(self.tstat)} "
            f"ratio_drift_max={_ffmt_or_dash(self.drift_max)} "
            f"anchor={self.anchor} rest={','.join(self.rest)}"
        )


@dataclass(frozen=True)
class HarvestPut:
    entry_id: str
    contract: Option
    expiration: str
    quantity: int
    limit_price: float
    gross_proceeds_per_contract: float
    estimated_fee_per_contract: float
    net_proceeds_per_contract: float
    cost_basis_per_contract: float


def _ffmt_or_dash(value: Optional[float], precision: int = 2) -> str:
    return ffmt(value, precision) if value is not None else "-"


def _pfmt_or_dash(value: Optional[float]) -> str:
    return pfmt(value) if value is not None else "-"


class RegimeHistoryCache:
    def __init__(self, fetcher: AlignedClosesFetcher) -> None:
        self._fetcher = fetcher
        self._cache: Dict[Tuple[Tuple[str, ...], int, int], AlignedClosesResult] = {}

    async def get(
        self,
        symbols: List[str],
        lookback_days: int,
        cooldown_days: int,
    ) -> AlignedClosesResult:
        key = (tuple(symbols), lookback_days, cooldown_days)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = await self._fetcher(symbols, lookback_days, cooldown_days)
        self._cache[key] = result
        return result


class RegimeRebalanceEngine:
    def __init__(
        self,
        *,
        config: Config,
        ibkr: IBKR,
        order_ops: OrderOperations,
        data_store: Optional[DataStore],
        get_primary_exchange: Callable[[str], str],
        now_provider: Callable[[], datetime],
        tail_hedge_stage_enabled: Callable[[], bool] | None = None,
        cash_management_stage_enabled: Callable[[], bool] | None = None,
        set_reserved_cash_for_post_management: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.order_ops = order_ops
        self.data_store = data_store
        self._get_primary_exchange = get_primary_exchange
        self._now = now_provider
        self._tail_hedge_stage_enabled = tail_hedge_stage_enabled or (lambda: False)
        self._cash_management_stage_enabled = cash_management_stage_enabled or (
            lambda: False
        )
        self._set_reserved_cash_for_post_management = (
            set_reserved_cash_for_post_management
        )
        self._tail_state_store = (
            TailHedgeStateStore(
                data_store,
                config.runtime.account.number,
            )
            if data_store is not None
            else None
        )
        self.regime_rebalance_order_ref_prefix = "tg:regime-rebalance"

    def _reserve_cash_for_post_management(self, amount: float) -> None:
        if self._set_reserved_cash_for_post_management is None:
            return
        self._set_reserved_cash_for_post_management(max(0.0, amount))

    def _estimated_tail_fee_per_contract(self) -> float:
        orders = getattr(self.config.runtime, "orders", None)
        return float(
            getattr(
                orders,
                "estimated_fee_per_contract",
                0.0,
            )
        )

    def _record_tail_harvest(
        self,
        outcome: str,
        *,
        symbol: str,
        **payload: Any,
    ) -> None:
        if self.data_store is None:
            return
        self.data_store.record_event(
            TAIL_HEDGE_HARVEST_EVENT,
            {
                "schema_version": TAIL_HEDGE_HARVEST_SCHEMA_VERSION,
                "account": self.config.runtime.account.number,
                "evaluated_at": self._now(),
                "symbol": symbol,
                "outcome": outcome,
                **payload,
            },
            symbol=symbol,
        )

    def _configured_tail_harvest_targets(self) -> set[str]:
        if self._tail_state_store is None or not self._tail_hedge_stage_enabled():
            return set()
        tail_hedge = self.config.strategies.tail_hedge
        if not tail_hedge.enabled:
            return set()
        return {
            target.symbol
            for target in tail_hedge.targets
            if self.config.trading_is_allowed(target.symbol)
        }

    def _load_tail_cohorts(self) -> list[TailHedgeCohort]:
        if self._tail_state_store is None:
            return []
        return self._tail_state_store.load().open_cohorts

    def _cash_fund_value(
        self,
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> float:
        cash_management = getattr(self.config.strategies, "cash_management", None)
        if (
            not bool(getattr(cash_management, "enabled", False))
            or not self._cash_management_stage_enabled()
        ):
            return 0.0
        cash_fund = getattr(cash_management, "cash_fund", None)
        if not isinstance(cash_fund, str) or not cash_fund:
            return 0.0

        value = 0.0
        for position in portfolio_positions.get(cash_fund, []):
            if not isinstance(position.contract, Stock) or (
                position.contract.symbol != cash_fund
            ):
                continue
            try:
                quantity = float(position.position)
                market_price = float(getattr(position, "marketPrice", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(quantity) or quantity <= 0:
                continue
            if not math.isfinite(market_price) or market_price <= 0:
                try:
                    market_value = float(getattr(position, "marketValue", 0.0) or 0.0)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(market_value) or market_value <= 0:
                    continue
                market_price = market_value / quantity
            if not math.isfinite(market_price) or market_price <= 0:
                continue
            value += math.floor(quantity) * market_price
        return value

    def _queued_cash_debits(self) -> PendingBuyCash:
        return pending_buy_cash(
            self.ibkr.open_trades(),
            self.order_ops.orders.records(),
            account=self.config.runtime.account.number,
        )

    def _ordinary_rebalance_shortfall(
        self,
        *,
        orders: List[Tuple[str, str, int]],
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
        market_prices: Dict[str, float],
    ) -> float:
        approved_buys = sum(
            quantity * market_prices[symbol]
            for symbol, _primary_exchange, quantity in orders
            if quantity > 0
        )
        approved_sells = sum(
            abs(quantity) * market_prices[symbol]
            for symbol, _primary_exchange, quantity in orders
            if quantity < 0
        )

        cash_value = account_summary.get("TotalCashValue")
        available_cash = float(getattr(cash_value, "value", 0.0) or 0.0)
        cash_management = getattr(self.config.strategies, "cash_management", None)
        cash_target = (
            float(getattr(cash_management, "target_cash_balance", 0.0) or 0.0)
            if bool(getattr(cash_management, "enabled", False))
            else 0.0
        )
        queued_cash = self._queued_cash_debits()
        if queued_cash.ambiguous:
            return 0.0
        cash_management_runs = (
            bool(getattr(cash_management, "enabled", False))
            and self._cash_management_stage_enabled()
        )
        sell_threshold = (
            float(getattr(cash_management, "sell_threshold", 0.0) or 0.0)
            if cash_management_runs
            else 0.0
        )
        cash_fund_value = self._cash_fund_value(portfolio_positions)
        cash_fund_symbol = getattr(cash_management, "cash_fund", None)
        cash_fund_sales = sum(
            abs(quantity) * market_prices[symbol]
            for symbol, _primary_exchange, quantity in orders
            if quantity < 0 and symbol == cash_fund_symbol
        )
        remaining_cash_fund_value = max(0.0, cash_fund_value - cash_fund_sales)

        # This is cash management's fixed point: after the retained buys, it
        # can liquidate the remaining fund and may leave at most its configured
        # deficit tolerance. Subtracting an approved fund sale above prevents
        # counting the same holding twice.
        ordinary_capacity = max(
            0.0,
            available_cash
            - cash_target
            - queued_cash.debit
            + approved_sells
            + remaining_cash_fund_value
            + sell_threshold,
        )
        return max(
            0.0,
            approved_buys - ordinary_capacity,
        )

    @staticmethod
    def _tail_hedge_market_value(
        portfolio_positions: Dict[str, List[PortfolioItem]],
        cohorts: list[TailHedgeCohort],
    ) -> float:
        owned_con_ids = {cohort.con_id for cohort in cohorts}
        return sum(
            float(position.marketValue or 0.0)
            for positions in portfolio_positions.values()
            for position in positions
            if isinstance(position.contract, Option)
            and position.contract.conId in owned_con_ids
        )

    def _working_option_orders(
        self,
        owned_con_ids: set[int] | None = None,
    ) -> tuple[set[int], set[str]]:
        account_number = self.config.runtime.account.number
        open_trades = self.ibkr.open_trades()
        if not isinstance(open_trades, list):
            return set(), set()
        con_ids: set[int] = set()
        tail_sell_symbols: set[str] = set()
        for trade in open_trades:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            is_done = getattr(trade, "isDone", None)
            if (
                (callable(is_done) and is_done())
                or order is None
                or getattr(order, "account", None) != account_number
                or not isinstance(contract, Option)
                or type(contract.conId) is not int
                or contract.conId <= 0
            ):
                continue
            con_ids.add(contract.conId)
            if (
                owned_con_ids is not None
                and contract.conId in owned_con_ids
                and str(getattr(order, "action", "")).upper() == "SELL"
                and is_tail_reduction_ref(getattr(order, "orderRef", None))
            ):
                tail_sell_symbols.add(str(contract.symbol))
        return con_ids, tail_sell_symbols

    def _tail_sale_in_progress_symbols(self, owned_con_ids: set[int]) -> set[str]:
        _working_con_ids, symbols = self._working_option_orders(owned_con_ids)
        account_number = self.config.runtime.account.number
        symbols.update(
            str(contract.symbol)
            for contract, order, _intent_id in self.order_ops.orders.records()
            if isinstance(contract, Option)
            and contract.conId in owned_con_ids
            and getattr(order, "account", None) == account_number
            and str(getattr(order, "action", "")).upper() == "SELL"
            and is_tail_reduction_ref(getattr(order, "orderRef", None))
        )
        return symbols

    def _live_account_puts(self) -> dict[int, PortfolioItem]:
        account_number = self.config.runtime.account.number
        positions = self.ibkr.portfolio(account=account_number)
        if not isinstance(positions, list):
            return {}
        return {
            int(position.contract.conId): position
            for position in positions
            if getattr(position, "account", None) == account_number
            and isinstance(position.contract, Option)
            and position.contract.right.upper().startswith("P")
            and type(position.contract.conId) is int
            and position.contract.conId > 0
            and float(position.position) > 0
        }

    async def _profitable_tail_puts(
        self,
        *,
        symbol: str,
        cohorts: list[TailHedgeCohort],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> list[HarvestPut]:
        snapshot_positions = {
            int(position.contract.conId): position
            for position in portfolio_positions.get(symbol, [])
            if isinstance(position.contract, Option)
            and position.contract.right.upper().startswith("P")
            and type(position.contract.conId) is int
            and position.contract.conId > 0
            and float(position.position) > 0
        }
        unavailable_con_ids, _harvest_symbols = self._working_option_orders()
        unavailable_con_ids.update(
            int(contract.conId)
            for contract, _order, _intent_id in self.order_ops.orders.records()
            if isinstance(contract, Option)
            and type(contract.conId) is int
            and contract.conId > 0
        )

        quoted: list[tuple[TailHedgeCohort, float]] = []
        for cohort in cohorts:
            con_id = cohort.con_id
            position = snapshot_positions.get(con_id)
            if (
                cohort.symbol != symbol
                or cohort.status != "active"
                or cohort.has_pending_recovery
                or con_id in unavailable_con_ids
                or position is None
            ):
                continue
            try:
                ticker = await self.ibkr.get_ticker_for_contract(
                    position.contract,
                    required_fields=[],
                    optional_fields=[
                        TickerField.MIDPOINT,
                        TickerField.MARKET_PRICE,
                    ],
                )
            except Exception as exc:
                log.warning(
                    f"{symbol}: Unable to quote tail put {con_id} for harvesting "
                    f"({type(exc).__name__})."
                )
                continue
            limit_price = round(float(midpoint_or_market_price(ticker)), 2)
            if math.isfinite(limit_price) and limit_price > 0:
                quoted.append((cohort, limit_price))

        # Quote requests yield to ib_async. Re-read its cache once after all
        # requests and before committing any close.
        live_positions = self._live_account_puts()
        unavailable_con_ids, _harvest_symbols = self._working_option_orders()
        candidates: list[HarvestPut] = []
        for cohort, limit_price in quoted:
            con_id = cohort.con_id
            position = live_positions.get(con_id)
            if (
                position is None
                or position.contract.symbol != symbol
                or con_id in unavailable_con_ids
            ):
                continue
            contract = position.contract
            if not isinstance(contract, Option):
                continue
            try:
                multiplier = float(contract.multiplier)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(multiplier) or multiplier <= 0:
                continue
            quantity = min(
                cohort.quantity,
                math.floor(float(position.position)),
            )
            if quantity <= 0:
                continue

            try:
                average_cost = float(getattr(position, "averageCost", 0.0) or 0.0)
            except (TypeError, ValueError):
                average_cost = 0.0
            configured_entry_basis = (
                cohort.entry_limit_price * multiplier
                + self._estimated_tail_fee_per_contract()
            )
            if not math.isfinite(average_cost) or average_cost <= 0:
                average_cost = configured_entry_basis
            else:
                average_cost = max(average_cost, configured_entry_basis)
            gross_proceeds = limit_price * multiplier
            estimated_fee = self._estimated_tail_fee_per_contract()
            net_proceeds = max(0.0, gross_proceeds - estimated_fee)
            if (
                not math.isfinite(average_cost)
                or average_cost <= 0
                or net_proceeds <= average_cost
            ):
                self._record_tail_harvest(
                    "candidate_not_net_profitable",
                    symbol=symbol,
                    entry_id=cohort.entry_id,
                    con_id=con_id,
                    gross_proceeds_per_contract=gross_proceeds,
                    estimated_fee_per_contract=estimated_fee,
                    net_proceeds_per_contract=net_proceeds,
                    cost_basis_per_contract=average_cost,
                )
                log.info(
                    f"{symbol}: Tail put conId={con_id} is not profitable after "
                    "its estimated sell fee."
                )
                continue

            contract.exchange = self.order_ops.get_order_exchange()
            candidates.append(
                HarvestPut(
                    entry_id=cohort.entry_id,
                    contract=contract,
                    expiration=cohort.expiration,
                    quantity=quantity,
                    limit_price=limit_price,
                    gross_proceeds_per_contract=gross_proceeds,
                    estimated_fee_per_contract=estimated_fee,
                    net_proceeds_per_contract=net_proceeds,
                    cost_basis_per_contract=average_cost,
                )
            )

        return sorted(
            candidates,
            key=lambda candidate: (
                candidate.expiration,
                -candidate.net_proceeds_per_contract,
                candidate.contract.conId,
            ),
        )

    async def _enqueue_tail_harvest(
        self,
        *,
        symbol: str,
        stock_price: float,
        deferred_shares: int,
        cohorts: list[TailHedgeCohort],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> bool:
        candidates = await self._profitable_tail_puts(
            symbol=symbol,
            cohorts=cohorts,
            portfolio_positions=portfolio_positions,
        )
        candidate = next(
            (
                item
                for item in candidates
                if math.floor(
                    item.quantity * item.net_proceeds_per_contract / stock_price
                )
                > 0
            ),
            None,
        )
        if candidate is None or deferred_shares <= 0:
            self._record_tail_harvest(
                "no_eligible_cohort",
                symbol=symbol,
                deferred_shares=deferred_shares,
                stock_price=stock_price,
            )
            return False

        # Monetize one useful tranche, shortest expiration first. A smaller
        # earlier tranche remains invested rather than blocking the ladder.
        total_proceeds = candidate.quantity * candidate.net_proceeds_per_contract
        fundable_shares = min(
            deferred_shares,
            math.floor(total_proceeds / stock_price),
        )
        required_proceeds = fundable_shares * stock_price
        quantity = min(
            candidate.quantity,
            math.ceil(required_proceeds / candidate.net_proceeds_per_contract),
        )
        estimated_net_proceeds = quantity * candidate.net_proceeds_per_contract
        if estimated_net_proceeds < required_proceeds:
            return False
        estimated_gross_proceeds = quantity * candidate.gross_proceeds_per_contract
        estimated_fees = quantity * candidate.estimated_fee_per_contract
        excess_proceeds = estimated_net_proceeds - required_proceeds

        con_id = candidate.contract.conId
        if self._tail_state_store is None:
            return False
        state = self._tail_state_store.load()
        state_cohort = state.find_open(candidate.entry_id, con_id)
        if (
            state_cohort is None
            or state_cohort.symbol != symbol
            or state_cohort.status != "active"
            or state_cohort.quantity < quantity
            or state_cohort.has_pending_recovery
        ):
            return False
        # Candidate sizing was refreshed from the live portfolio after quotes.
        # A prior external reduction is not proceeds from this unsubmitted sale.
        state_cohort.quantity = min(state_cohort.quantity, candidate.quantity)
        state_cohort.begin_recovery(
            quantity=quantity,
            proceeds_per_contract=round(candidate.net_proceeds_per_contract, 2),
            enqueued_at=self._now(),
        )
        try:
            self._tail_state_store.save(state)
        except RuntimeError:
            log.error(
                f"{symbol}: Unable to persist tail-harvest recovery intent; "
                "keeping the ordinary rebalance order unchanged."
            )
            return False
        order = self.order_ops.create_limit_order(
            action="SELL",
            quantity=quantity,
            limit_price=candidate.limit_price,
            use_default_algo=False,
            order_ref=build_tail_reduction_order_ref(
                f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:{symbol}",
                con_id,
                state_cohort.pending_recovery_enqueued_at,
            ),
            transmit=True,
        )
        self.order_ops.enqueue_order(candidate.contract, order)
        self._record_tail_harvest(
            "harvest_enqueued",
            symbol=symbol,
            entry_id=candidate.entry_id,
            con_id=con_id,
            expiration=candidate.expiration,
            quantity=quantity,
            limit_price=candidate.limit_price,
            cost_basis_per_contract=candidate.cost_basis_per_contract,
            gross_proceeds=estimated_gross_proceeds,
            estimated_fees=estimated_fees,
            net_proceeds=estimated_net_proceeds,
            required_proceeds=required_proceeds,
            excess_proceeds=excess_proceeds,
            stock_price=stock_price,
            deferred_shares=deferred_shares,
            fundable_shares=fundable_shares,
        )
        log.notice(
            f"{symbol}: Harvesting {quantity} profitable tail put(s) from one "
            f"tranche for about {dfmt(estimated_net_proceeds)} net of estimated "
            f"fees; excess over this rebalance slice={dfmt(excess_proceeds)}, "
            f"fundable deferred shares={fundable_shares}."
        )
        return True

    async def _apply_tail_harvest(
        self,
        *,
        orders: List[Tuple[str, str, int]],
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
        market_prices: Dict[str, float],
        regime_summary: List[Dict[str, Any]],
        hard_underweight_symbols: set[str],
        cohorts: list[TailHedgeCohort],
    ) -> List[Tuple[str, str, int]]:
        targets = self._configured_tail_harvest_targets()
        if not targets or not cohorts or not hard_underweight_symbols:
            return orders

        # Re-materialize ib_async's fill-current caches after preceding awaits;
        # no broker refresh request is needed.
        account_number = self.config.runtime.account.number
        try:
            live_cash = self.ibkr.cached_account_value(account_number, "TotalCashValue")
        except RuntimeError:
            return orders
        account_summary = dict(account_summary)
        account_summary["TotalCashValue"] = AccountValue(
            account_number, "TotalCashValue", str(live_cash), "BASE", ""
        )
        portfolio_positions = portfolio_positions_to_dict(
            self.ibkr.portfolio(account=account_number)
        )

        funding_shortfall = self._ordinary_rebalance_shortfall(
            orders=orders,
            account_summary=account_summary,
            portfolio_positions=portfolio_positions,
            market_prices=market_prices,
        )
        if funding_shortfall <= 0:
            return orders

        owned_con_ids = {cohort.con_id for cohort in cohorts}
        working_sale_symbols = self._tail_sale_in_progress_symbols(owned_con_ids)
        pending_recovery_symbols = {
            cohort.symbol for cohort in cohorts if cohort.has_pending_recovery
        }
        blocked_symbols = working_sale_symbols | pending_recovery_symbols
        summaries = {str(item["symbol"]): item for item in regime_summary}
        eligible_buys = {
            symbol: (quantity, quantity * market_prices[symbol])
            for symbol, _primary_exchange, quantity in orders
            if quantity > 0 and symbol in targets and symbol in hard_underweight_symbols
        }
        eligible_buy_value = sum(value for _quantity, value in eligible_buys.values())
        if eligible_buy_value <= 0:
            return orders
        allocatable_shortfall = min(funding_shortfall, eligible_buy_value)
        dollar_allocations = {
            symbol: allocatable_shortfall * buy_value / eligible_buy_value
            for symbol, (_quantity, buy_value) in eligible_buys.items()
        }
        deferred_by_symbol = {
            symbol: min(
                quantity,
                math.floor(dollar_allocations[symbol] / market_prices[symbol]),
            )
            for symbol, (quantity, _buy_value) in eligible_buys.items()
        }
        deferred_value = sum(
            quantity * market_prices[symbol]
            for symbol, quantity in deferred_by_symbol.items()
        )
        # Largest-remainder apportionment keeps scarce cash proportional in
        # dollars; symbol is only the deterministic tie-break for whole shares.
        remainder_order = sorted(
            eligible_buys,
            key=lambda symbol: (
                -(
                    dollar_allocations[symbol] / market_prices[symbol]
                    - deferred_by_symbol[symbol]
                ),
                symbol,
            ),
        )
        for symbol in remainder_order:
            if deferred_value >= allocatable_shortfall or math.isclose(
                deferred_value,
                allocatable_shortfall,
                abs_tol=1e-6,
            ):
                break
            quantity = eligible_buys[symbol][0]
            if deferred_by_symbol[symbol] >= quantity:
                continue
            deferred_by_symbol[symbol] += 1
            deferred_value += market_prices[symbol]

        retained_orders: list[tuple[str, str, int]] = []
        for symbol, primary_exchange, quantity in orders:
            if (
                quantity <= 0
                or symbol not in targets
                or symbol not in hard_underweight_symbols
            ):
                retained_orders.append((symbol, primary_exchange, quantity))
                continue

            stock_price = market_prices[symbol]
            allocated_deferred_shares = deferred_by_symbol.get(symbol, 0)
            if allocated_deferred_shares <= 0:
                retained_orders.append((symbol, primary_exchange, quantity))
                continue
            if symbol in blocked_symbols:
                # Do not spend estimated proceeds from a harvest that is still
                # working or awaiting reconciliation. Keep only the
                # ordinary-funded stock slice; a later invocation starts from a
                # newly synchronized broker snapshot.
                harvest_pending = True
                reason = (
                    "pending_recovery"
                    if symbol in pending_recovery_symbols
                    else "working_tail_sale"
                )
                self._record_tail_harvest(
                    "harvest_deferred",
                    symbol=symbol,
                    reason=reason,
                    deferred_shares=allocated_deferred_shares,
                    funding_shortfall=funding_shortfall,
                    live_cash=live_cash,
                )
                log.info(
                    f"{symbol}: Deferring {allocated_deferred_shares} stock "
                    f"share(s); tail harvest blocked by {reason}."
                )
            else:
                harvest_pending = await self._enqueue_tail_harvest(
                    symbol=symbol,
                    stock_price=stock_price,
                    deferred_shares=allocated_deferred_shares,
                    cohorts=cohorts,
                    portfolio_positions=portfolio_positions,
                )
            deferred_shares = allocated_deferred_shares if harvest_pending else 0
            retained_quantity = quantity - deferred_shares
            if retained_quantity > 0:
                retained_orders.append((symbol, primary_exchange, retained_quantity))
            summary = summaries.get(symbol)
            if summary is not None and deferred_shares > 0:
                summary["shares_to_trade"] = retained_quantity
                summary["action"] = (
                    f"[green]Buy {retained_quantity}; "
                    f"[magenta]defer {deferred_shares} to tail proceeds"
                    if retained_quantity > 0
                    else f"[magenta]Harvest puts; defer {deferred_shares}"
                )

        return retained_orders

    @staticmethod
    def _as_int_or_none(value: Any) -> int | None:
        return value if isinstance(value, int) else None

    @staticmethod
    def _as_float_or_none(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    @classmethod
    def _resolve_ratio_gate_vol_min(cls, ratio_gate: Any) -> float:
        vol_min = cls._as_float_or_none(getattr(ratio_gate, "vol_min", None))
        if vol_min is None:
            return 0.0
        return max(vol_min, 0.0)

    @staticmethod
    def _normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
        total_weight = sum(weights.values())
        if total_weight <= 0:
            raise ValueError("weights must sum to a positive value")
        return {symbol: weight / total_weight for symbol, weight in weights.items()}

    @staticmethod
    def _weighted_return_index(
        symbols: List[str],
        weights: Dict[str, float],
        aligned_closes: Dict[str, List[float]],
        length: int,
        eps: float = 0.0,
    ) -> List[float]:
        index = [1.0]
        for idx in range(1, length):
            daily_factor = 0.0
            for symbol in symbols:
                prev_close = max(aligned_closes[symbol][idx - 1], eps)
                curr_close = max(aligned_closes[symbol][idx], eps)
                daily_factor += weights[symbol] * (curr_close / prev_close)
            index.append(index[-1] * max(daily_factor, eps))
        return index

    @staticmethod
    def _bars_to_closes(bars: Iterable[Any]) -> Dict[date, float]:
        closes: Dict[date, float] = {}
        for bar in bars:
            bar_date = bar.date.date() if hasattr(bar.date, "date") else bar.date
            closes[bar_date] = float(bar.close)
        return closes

    @staticmethod
    def _describe_history_closes(closes: Dict[date, float]) -> str:
        if not closes:
            return "0 bars"
        sorted_dates = sorted(closes)
        return f"{len(sorted_dates)} bars {sorted_dates[0]}..{sorted_dates[-1]}"

    @classmethod
    def _align_regime_closes(
        cls,
        *,
        symbols: List[str],
        closes_by_symbol: ClosesBySymbol,
        required_points: int,
        required_dates: List[date],
        missing_dates_cache_recoverable: bool,
    ) -> AlignedClosesResult:
        close_dates_by_symbol = {
            symbol: set(closes_by_symbol.get(symbol, {})) for symbol in symbols
        }
        common_dates = set.intersection(*close_dates_by_symbol.values())
        if not common_dates:
            details = ", ".join(
                (
                    f"{symbol}: "
                    f"{cls._describe_history_closes(closes_by_symbol.get(symbol, {}))}"
                )
                for symbol in symbols
            )
            log.error(
                "Regime-aware rebalancing history has no common dates across "
                f"symbols ({details})."
            )
            raise RegimeHistoryValidationError(
                "Regime-aware rebalancing requires aligned history for all symbols.",
                cache_recoverable=True,
            )

        missing_dates = [
            required_date
            for required_date in required_dates
            if required_date not in common_dates
        ]
        if missing_dates:
            sample = ", ".join(str(missing) for missing in missing_dates[:5])
            if len(missing_dates) > 5:
                sample += ", ..."
            log.error(
                f"Regime history is missing required completed sessions: {sample}."
            )
            raise RegimeHistoryValidationError(
                "Regime-aware rebalancing requires fresh historical data.",
                cache_recoverable=missing_dates_cache_recoverable,
            )
        sorted_dates = sorted(required_dates)
        if len(sorted_dates) < required_points:
            log.error(
                "Insufficient historical data for regime rebalancing "
                f"({len(sorted_dates)}/{required_points} common points), aborting."
            )
            raise RegimeHistoryValidationError(
                "Regime-aware rebalancing requires full lookback history.",
                cache_recoverable=True,
            )

        aligned_closes: Dict[str, List[float]] = {}
        for symbol in symbols:
            aligned: List[float] = []
            for date_point in sorted_dates:
                close = closes_by_symbol[symbol].get(date_point)
                if close is None or math.isnan(close) or math.isclose(close, 0):
                    log.error(
                        f"Invalid close for {symbol} on {date_point} (close={close})."
                    )
                    raise RegimeHistoryValidationError(
                        "Regime-aware rebalancing found invalid historical closes.",
                        cache_recoverable=False,
                    )
                aligned.append(close)
            aligned_closes[symbol] = aligned

        return (sorted_dates, aligned_closes)

    def _calculate_ratio_gate(
        self,
        *,
        symbols: List[str],
        dates: List[date],
        aligned_closes: Dict[str, List[float]],
        ratio_gate: Any,
        effective_weights: Dict[str, float],
        lookback_days: int,
        eps: float,
    ) -> RatioGateResult:
        ratio_anchor = getattr(ratio_gate, "anchor", "")
        ratio_rest = [symbol for symbol in symbols if symbol != ratio_anchor]
        if not ratio_anchor or ratio_anchor not in symbols or not ratio_rest:
            log.error("Regime-aware ratio gate has invalid anchor configuration.")
            raise ValueError(
                "Regime-aware ratio gate requires a valid anchor and rest basket."
            )

        rest_weights = {symbol: effective_weights[symbol] for symbol in ratio_rest}
        try:
            normalized_rest_weights = self._normalize_weights(rest_weights)
        except ValueError:
            log.error("Ratio gate rest weights sum to zero, skipping.")
            raise ValueError(
                "Regime-aware ratio gate requires positive weights."
            ) from None

        rest_index = self._weighted_return_index(
            ratio_rest,
            normalized_rest_weights,
            aligned_closes,
            len(dates),
            eps,
        )
        anchor_index = self._weighted_return_index(
            [ratio_anchor],
            {ratio_anchor: 1.0},
            aligned_closes,
            len(dates),
            eps,
        )

        ratio_series = np.log(np.array(rest_index) / np.array(anchor_index))
        ratio_returns = pd.Series(ratio_series).diff()
        rolling_returns = ratio_returns.rolling(lookback_days)
        ratio_var = float(rolling_returns.var(ddof=1).iloc[-1])
        ratio_mean = float(rolling_returns.mean().iloc[-1])
        ratio_std = float(rolling_returns.std(ddof=1).iloc[-1])
        ratio_vol_min = self._resolve_ratio_gate_vol_min(ratio_gate)
        ratio_drift_max = float(getattr(ratio_gate, "drift_max", 0.0))

        if math.isnan(ratio_var) or math.isnan(ratio_mean) or math.isnan(ratio_std):
            return RatioGateResult(
                ok=False,
                reason="insufficient_history",
                anchor=ratio_anchor,
                rest=ratio_rest,
                weights=normalized_rest_weights,
                daily_mean=None,
                daily_std=None,
                daily_var=None,
                annualized_vol=None,
                vol_min=ratio_vol_min,
                tstat=float("inf"),
                drift_max=ratio_drift_max,
            )

        if ratio_std <= 0:
            ratio_tstat = float("inf")
        else:
            ratio_tstat = abs(ratio_mean / (ratio_std / math.sqrt(lookback_days)))
        annualized_vol = ratio_std * math.sqrt(TRADING_DAYS_PER_YEAR)

        if ratio_std <= 0:
            reason = "zero_volatility"
        elif annualized_vol < ratio_vol_min:
            reason = "vol_below_min"
        elif ratio_tstat > ratio_drift_max:
            reason = "drift_above_max"
        else:
            reason = "ok"

        return RatioGateResult(
            ok=reason == "ok",
            reason=reason,
            anchor=ratio_anchor,
            rest=ratio_rest,
            weights=normalized_rest_weights,
            daily_mean=ratio_mean,
            daily_std=ratio_std,
            daily_var=ratio_var,
            annualized_vol=annualized_vol,
            vol_min=ratio_vol_min,
            tstat=ratio_tstat,
            drift_max=ratio_drift_max,
        )

    def _resolve_regime_margin_usage(self) -> float:
        fallback_raw = self.config.runtime.account.margin_usage
        fallback = (
            float(fallback_raw)
            if isinstance(fallback_raw, (int, float))
            and not isinstance(fallback_raw, bool)
            else 1.0
        )
        resolver = getattr(self.config, "regime_margin_usage", None)
        if not callable(resolver):
            return fallback
        try:
            resolved = resolver()
        except Exception:
            return fallback
        if isinstance(resolved, bool) or not isinstance(resolved, (int, float)):
            return fallback
        return float(resolved)

    def get_primary_exchange(self, symbol: str) -> str:
        return self._get_primary_exchange(symbol)

    async def _get_regime_proxy_series(
        self,
        symbols: List[str],
        lookback_days: int,
        cooldown_days: int,
        weights_override: Optional[Dict[str, float]] = None,
        history_cache: Optional[RegimeHistoryCache] = None,
    ) -> Tuple[List[date], List[float]]:
        symbol_configs = resolve_symbol_configs(
            self.config, context="regime proxy series"
        )
        proxy_symbols = list(weights_override.keys()) if weights_override else symbols
        if history_cache is None:
            sorted_dates, aligned_closes = await self._get_regime_aligned_closes(
                symbols,
                lookback_days,
                cooldown_days,
            )
        else:
            sorted_dates, aligned_closes = await history_cache.get(
                symbols,
                lookback_days,
                cooldown_days,
            )

        if weights_override:
            weights = weights_override
        else:
            weights = {
                symbol: symbol_configs[symbol].weight for symbol in proxy_symbols
            }
        try:
            normalized_weights = self._normalize_weights(weights)
        except ValueError:
            log.error("Regime-aware rebalancing weights sum to zero, skipping.")
            raise ValueError(
                "Regime-aware rebalancing weights must sum to a positive value."
            ) from None

        normalized_series = self._weighted_return_index(
            proxy_symbols,
            normalized_weights,
            aligned_closes,
            len(sorted_dates),
        )
        return (sorted_dates, normalized_series)

    def _get_required_history_dates(self, required_points: int) -> Optional[List[date]]:
        if required_points <= 0:
            return []
        try:
            exchange = self.config.runtime.exchange_hours.exchange
            calendar = xcals.get_calendar(exchange)
            now = pd.Timestamp.now(tz="UTC")
            today = now.tz_convert(None).normalize()
            sessions = calendar.sessions[calendar.sessions <= today]
            if sessions.empty:
                return None

            latest_session = sessions[-1]
            latest_close = calendar.session_close(latest_session, _parse=False)
            if now < latest_close:
                latest_index = calendar.sessions.get_loc(latest_session)
                if latest_index == 0:
                    return None
                latest_session = calendar.sessions[latest_index - 1]

            latest_index = calendar.sessions.get_loc(latest_session)
            first_index = latest_index - required_points + 1
            if first_index < 0:
                return None
            return [
                session.date()
                for session in calendar.sessions[first_index : latest_index + 1]
            ]
        except Exception as exc:
            log.warning(
                f"Regime history freshness calculation failed ({type(exc).__name__})."
            )
            return None

    async def _fetch_regime_history_bars(
        self, symbol: str, duration: str
    ) -> Tuple[str, List[Any]]:
        contract = Stock(
            symbol,
            self.order_ops.get_order_exchange(),
            currency="USD",
            primaryExchange=self.get_primary_exchange(symbol),
        )
        for attempt in range(1, REGIME_HISTORY_MAX_ATTEMPTS + 1):
            bars = list(await self.ibkr.request_historical_data(contract, duration))
            if bars:
                if attempt > 1:
                    log.warning(
                        f"{symbol}: regime history fetch recovered after "
                        f"{attempt} attempts."
                    )
                return symbol, bars
            if attempt < REGIME_HISTORY_MAX_ATTEMPTS:
                log.warning(
                    f"{symbol}: regime history fetch returned no bars "
                    f"(attempt {attempt}/{REGIME_HISTORY_MAX_ATTEMPTS}); retrying."
                )
                await asyncio.sleep(REGIME_HISTORY_RETRY_DELAY_SECONDS)
        log.error(
            f"{symbol}: regime history fetch returned no bars after "
            f"{REGIME_HISTORY_MAX_ATTEMPTS} attempts."
        )
        return symbol, []

    async def _fetch_regime_history_closes(
        self, symbols: List[str], duration: str
    ) -> ClosesBySymbol:
        tasks: List[Coroutine[Any, Any, Tuple[str, List[Any]]]] = [
            self._fetch_regime_history_bars(symbol, duration) for symbol in symbols
        ]
        histories = await log.track_async(
            tasks, description="Fetching regime rebalancing history..."
        )
        return {symbol: self._bars_to_closes(bars) for symbol, bars in histories}

    def _merge_cached_regime_closes(
        self,
        symbols: List[str],
        api_closes_by_symbol: ClosesBySymbol,
        required_dates: List[date],
    ) -> ClosesBySymbol:
        if self.data_store is None:
            raise RegimeHistoryValidationError(
                "Regime-aware rebalancing requires a history cache.",
                cache_recoverable=False,
            )
        start_time = datetime.combine(required_dates[0], datetime.min.time())
        end_time = datetime.combine(required_dates[-1], datetime.max.time())
        merged_closes_by_symbol: ClosesBySymbol = {}
        for symbol in symbols:
            try:
                cached_bars = self.data_store.get_historical_bars(
                    symbol, REGIME_HISTORY_TIMEFRAME, start_time, end_time
                )
            except Exception as exc:
                log.error(f"{symbol}: failed to read cached regime history.")
                raise RegimeHistoryValidationError(
                    "Regime-aware rebalancing requires a readable history cache.",
                    cache_recoverable=False,
                ) from exc
            cached_closes = self._bars_to_closes(cached_bars)
            if cached_closes:
                log.warning(
                    f"{symbol}: using {len(cached_closes)} cached historical bars "
                    "to validate regime history."
                )
            merged = dict(cached_closes)
            merged.update(api_closes_by_symbol[symbol])
            merged_closes_by_symbol[symbol] = merged
        return merged_closes_by_symbol

    def _recover_regime_history_from_cache(
        self,
        *,
        symbols: List[str],
        api_closes_by_symbol: ClosesBySymbol,
        required_points: int,
        required_dates: List[date],
    ) -> AlignedClosesResult:
        merged_closes_by_symbol = self._merge_cached_regime_closes(
            symbols,
            api_closes_by_symbol,
            required_dates,
        )
        dates, aligned_closes = self._align_regime_closes(
            symbols=symbols,
            closes_by_symbol=merged_closes_by_symbol,
            required_points=required_points,
            required_dates=required_dates,
            missing_dates_cache_recoverable=False,
        )
        log.warning(
            "Regime-aware rebalancing recovered from incomplete API history "
            "using fresh cached bars."
        )
        return (dates, aligned_closes)

    async def _get_regime_aligned_closes(
        self,
        symbols: List[str],
        lookback_days: int,
        cooldown_days: int,
    ) -> Tuple[List[date], Dict[str, List[float]]]:
        if not symbols:
            log.error("Regime-aware rebalancing has no symbols to build a proxy.")
            raise ValueError("Regime-aware rebalancing requires proxy symbols.")
        required_points = lookback_days + 1
        required_dates = self._get_required_history_dates(required_points)
        if required_dates is None:
            raise RegimeHistoryValidationError(
                "Regime-aware rebalancing requires completed session dates.",
                cache_recoverable=False,
            )
        trading_days_needed = lookback_days + 1 + max(cooldown_days, 0)
        calendar_days = math.ceil(trading_days_needed * 7 / 5) + 5
        duration = f"{calendar_days} D"
        api_closes_by_symbol = await self._fetch_regime_history_closes(
            symbols, duration
        )

        try:
            return self._align_regime_closes(
                symbols=symbols,
                closes_by_symbol=api_closes_by_symbol,
                required_points=required_points,
                required_dates=required_dates,
                missing_dates_cache_recoverable=True,
            )
        except RegimeHistoryValidationError as api_exc:
            if not api_exc.cache_recoverable or self.data_store is None:
                raise
            return self._recover_regime_history_from_cache(
                symbols=symbols,
                api_closes_by_symbol=api_closes_by_symbol,
                required_points=required_points,
                required_dates=required_dates,
            )

    async def _resolve_effective_weights(
        self,
        symbols: List[str],
        symbol_configs: Dict[str, Any],
        history_cache: Optional[RegimeHistoryCache] = None,
    ) -> Tuple[Dict[str, float], Dict[str, Dict[str, float]]]:
        effective_weights = {
            symbol: float(symbol_configs[symbol].weight) for symbol in symbols
        }
        volatility_details: Dict[str, Dict[str, float]] = {}
        previous_state = (
            self.data_store.get_last_event_payload("volatility_weight_state")
            if self.data_store
            else None
        )
        previous_symbols = (
            previous_state.get("symbols", {})
            if isinstance(previous_state, dict)
            else {}
        )
        volatility_symbols_by_lookback: Dict[int, List[str]] = {}

        for symbol in symbols:
            symbol_config = symbol_configs[symbol]
            volatility_weight = getattr(symbol_config, "volatility_weight", None)
            if volatility_weight is None or not getattr(
                volatility_weight, "enabled", False
            ):
                continue
            lookback_days = int(volatility_weight.lookback_days)
            volatility_symbols_by_lookback.setdefault(lookback_days, []).append(symbol)

        for lookback_days, group_symbols in volatility_symbols_by_lookback.items():
            try:
                if history_cache is None:
                    _, aligned_closes = await self._get_regime_aligned_closes(
                        group_symbols,
                        lookback_days,
                        0,
                    )
                else:
                    _, aligned_closes = await history_cache.get(
                        group_symbols,
                        lookback_days,
                        0,
                    )
            except Exception as exc:
                for symbol in group_symbols:
                    log.warning(
                        f"{symbol}: volatility weight history fetch failed ({type(exc).__name__}); using static weight."
                    )
                continue

            for symbol in group_symbols:
                symbol_config = symbol_configs[symbol]
                volatility_weight = getattr(symbol_config, "volatility_weight", None)
                if volatility_weight is None:
                    continue
                base_weight = float(symbol_config.weight)
                try:
                    closes = aligned_closes[symbol]
                    if len(closes) < lookback_days + 1:
                        log.warning(
                            f"{symbol}: volatility weight has insufficient history; using static weight."
                        )
                        continue

                    window = np.array(closes[-(lookback_days + 1) :], dtype=float)
                    if np.any(window <= 0) or np.any(np.isnan(window)):
                        log.warning(
                            f"{symbol}: volatility weight found invalid closes; using static weight."
                        )
                        continue

                    returns = np.diff(np.log(window))
                    realized_vol = float(np.std(returns, ddof=1) * math.sqrt(252))
                    if math.isnan(realized_vol) or realized_vol <= 0:
                        log.warning(
                            f"{symbol}: volatility weight realized vol is invalid; using static weight."
                        )
                        continue

                    max_weight = float(volatility_weight.max_weight)
                    raw_weight = (
                        base_weight * float(volatility_weight.target_vol) / realized_vol
                    )
                    target_weight = max(
                        float(volatility_weight.min_weight),
                        min(raw_weight, max_weight),
                    )
                    previous_symbol_state = previous_symbols.get(symbol, {})
                    previous_weight_raw = (
                        previous_symbol_state.get("effective_weight")
                        if isinstance(previous_symbol_state, dict)
                        else None
                    )
                    previous_weight = (
                        float(previous_weight_raw)
                        if isinstance(previous_weight_raw, (int, float))
                        and not isinstance(previous_weight_raw, bool)
                        else base_weight
                    )
                    smoothing_factor = float(volatility_weight.smoothing_factor)
                    if target_weight > previous_weight:
                        smoothing_factor = float(
                            volatility_weight.increase_smoothing_factor
                            if volatility_weight.increase_smoothing_factor is not None
                            else smoothing_factor
                        )
                    elif target_weight < previous_weight:
                        smoothing_factor = float(
                            volatility_weight.decrease_smoothing_factor
                            if volatility_weight.decrease_smoothing_factor is not None
                            else smoothing_factor
                        )

                    rebalance_band = float(volatility_weight.rebalance_band)
                    if abs(target_weight - previous_weight) < rebalance_band:
                        effective_weight = previous_weight
                    else:
                        effective_weight = previous_weight + (
                            smoothing_factor * (target_weight - previous_weight)
                        )
                    effective_weight = max(
                        float(volatility_weight.min_weight),
                        min(effective_weight, max_weight),
                    )
                    effective_weights[symbol] = effective_weight
                    volatility_details[symbol] = {
                        "base_weight": base_weight,
                        "effective_weight": effective_weight,
                        "realized_vol": realized_vol,
                        "raw_weight": raw_weight,
                        "target_weight": target_weight,
                        "previous_weight": previous_weight,
                        "smoothing_factor": smoothing_factor,
                    }
                    log.notice(
                        f"{symbol}: volatility weight base={pfmt(base_weight)} "
                        f"raw={pfmt(raw_weight)} "
                        f"realized_vol={pfmt(realized_vol)} "
                        f"clamped={pfmt(target_weight)} "
                        f"previous={pfmt(previous_weight)} "
                        f"smoothing={ffmt(smoothing_factor)} "
                        f"effective={pfmt(effective_weight)}"
                    )
                except Exception as exc:
                    log.warning(
                        f"{symbol}: volatility weight calculation failed ({type(exc).__name__}); using static weight."
                    )

        return effective_weights, volatility_details

    async def _get_last_regime_rebalance_time(
        self, symbols: List[str]
    ) -> Optional[datetime]:
        regime_rebalance = self.config.strategies.regime_rebalance
        if not regime_rebalance.enabled:
            return None

        lookback_days = max(regime_rebalance.order_history_lookback_days, 1)
        start_time = self._now() - timedelta(days=lookback_days)
        account_number = self.config.runtime.account.number
        exec_filter = ExecutionFilter(
            acctCode=account_number,
            time=start_time.strftime("%Y%m%d %H:%M:%S"),
        )

        if self.data_store:
            refresh_failed = False
            fills: Iterable[Any] = ()
            try:
                fills = await self.ibkr.request_executions(exec_filter)
            except Exception as exc:
                refresh_failed = True
                log.warning(
                    "Unable to refresh executions for regime rebalancing "
                    f"({type(exc).__name__}); using persisted execution history."
                )
            last_rebalance = self.data_store.get_last_regime_rebalance_time(
                symbols,
                self.regime_rebalance_order_ref_prefix,
                start_time,
                account_number,
                # Pre-account migrations cannot attribute old rows. Exact
                # account history wins; otherwise the legacy row is the safer
                # cooldown anchor until IBKR refreshes it.
                include_legacy_unscoped=True,
            )
            if refresh_failed and last_rebalance is None:
                log.warning(
                    "Execution history is unavailable; deferring regime rebalancing."
                )
                return self._now()
            live_rebalance = self._last_regime_fill_time(fills, symbols)
            if live_rebalance is None:
                return last_rebalance
            if last_rebalance is None:
                return live_rebalance
            return max(last_rebalance, live_rebalance)

        fills = await self.ibkr.request_executions(exec_filter)
        return self._last_regime_fill_time(fills, symbols)

    def _last_regime_fill_time(
        self,
        fills: Iterable[Any],
        symbols: Iterable[str],
    ) -> Optional[datetime]:
        symbols = set(symbols)
        last_rebalance: Optional[datetime] = None
        for fill in fills:
            execution = getattr(fill, "execution", None)
            order_ref = getattr(execution, "orderRef", None)
            if not isinstance(order_ref, str):
                continue
            if not order_ref.startswith(self.regime_rebalance_order_ref_prefix):
                continue
            if getattr(getattr(fill, "contract", None), "symbol", None) not in symbols:
                continue
            account = getattr(execution, "acctNumber", None)
            if account not in {None, "", self.config.runtime.account.number}:
                continue
            fill_time = parse_state_datetime(
                getattr(fill, "time", None) or getattr(execution, "time", None)
            )
            if fill_time is None:
                continue
            if last_rebalance is None or fill_time > last_rebalance:
                last_rebalance = fill_time

        return last_rebalance

    def _cooldown_elapsed(self, last_rebalance: datetime, cooldown_days: int) -> bool:
        if cooldown_days <= 0:
            return True

        now = self._now()
        if last_rebalance >= now:
            return False

        start_date = last_rebalance.date()
        end_date = now.date()
        if end_date < start_date:
            return False

        try:
            exchange = self.config.runtime.exchange_hours.exchange
            calendar = xcals.get_calendar(exchange)
            start_ts = pd.Timestamp(start_date)
            end_ts = pd.Timestamp(end_date)
            sessions = calendar.sessions
            sessions = sessions[(sessions >= start_ts) & (sessions <= end_ts)]
            if sessions.empty:
                raise ValueError("No exchange sessions found in cooldown window.")
            session_dates = [session.date() for session in sessions]
            sessions_after = [d for d in session_dates if d > start_date]
            return len(sessions_after) >= cooldown_days
        except Exception as exc:
            log.warning(
                "Regime rebalancing cooldown calculation failed "
                f"({type(exc).__name__}); using calendar days."
            )
            return (end_date - start_date).days >= cooldown_days

    async def check_regime_rebalance_positions(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Tuple[Table, List[Tuple[str, str, int]]]:
        symbol_configs = resolve_symbol_configs(
            self.config, context="regime rebalance check"
        )
        table = Table(title="Regime-aware rebalancing summary")
        table.add_column("Symbol")
        table.add_column("Weights", justify="right")
        table.add_column("Value", justify="right")
        table.add_column("Shares", justify="right")
        table.add_column("Gate", justify="center")
        table.add_column("Action")

        to_trade: List[Tuple[str, str, int]] = []
        regime_rebalance = self.config.strategies.regime_rebalance
        if not regime_rebalance.enabled:
            return (table, to_trade)

        symbols = list(regime_rebalance.symbols)
        if not symbols:
            log.warning(
                "Regime-aware rebalancing enabled but no symbols are configured."
            )
            return (table, to_trade)
        configured_regime_symbols = set(symbols)

        missing_symbols = [symbol for symbol in symbols if symbol not in symbol_configs]
        if missing_symbols:
            log.error(
                f"Regime-aware rebalancing symbols missing from config: {', '.join(missing_symbols)}"
            )
            raise ValueError(
                "Regime-aware rebalancing requires symbols present in config."
            )

        zero_weight_symbols = [
            symbol for symbol in symbols if symbol_configs[symbol].weight <= 0
        ]
        if zero_weight_symbols:
            log.warning(
                "Regime-aware rebalancing ignoring zero-weight symbols: "
                f"{', '.join(zero_weight_symbols)}"
            )
        symbols = [symbol for symbol in symbols if symbol_configs[symbol].weight > 0]
        if not symbols:
            log.error("Regime-aware rebalancing has no positive-weight symbols.")
            raise ValueError(
                "Regime-aware rebalancing requires positive target weights."
            )

        stock_positions = [
            position
            for symbol in portfolio_positions
            for position in portfolio_positions[symbol]
            if isinstance(position.contract, Stock)
        ]
        stock_symbols: Dict[str, PortfolioItem] = {
            position.contract.symbol: position for position in stock_positions
        }

        async def get_ticker_task(symbol: str) -> Tuple[str, Ticker]:
            ticker = await self.ibkr.get_ticker_for_stock(
                symbol, self.get_primary_exchange(symbol)
            )
            return symbol, ticker

        ticker_tasks: List[Coroutine[Any, Any, Tuple[str, Ticker]]] = [
            get_ticker_task(symbol) for symbol in symbols
        ]
        ticker_results = await log.track_async(
            ticker_tasks, description="Fetching regime rebalancing prices..."
        )
        tickers = {symbol: ticker for symbol, ticker in ticker_results}

        current_positions: Dict[str, int] = {}
        current_values: Dict[str, float] = {}
        market_prices: Dict[str, float] = {}
        target_shares: Dict[str, int] = {}
        target_values: Dict[str, float] = {}
        relative_ratios: Dict[str, float] = {}
        relative_drifts: Dict[str, float] = {}
        share_gaps: Dict[str, int] = {}
        for symbol in symbols:
            ticker = tickers[symbol]
            market_price = ticker.marketPrice()
            if (
                not market_price
                or math.isnan(market_price)
                or math.isclose(market_price, 0)
            ):
                log.error(
                    f"Invalid market price for {symbol} (market_price={market_price}), skipping for now"
                )
                raise ValueError(
                    "Regime-aware rebalancing requires valid market prices."
                )
            market_prices[symbol] = market_price

            current_position = math.floor(
                stock_symbols[symbol].position if symbol in stock_symbols else 0
            )
            current_positions[symbol] = current_position
            current_value = current_position * market_price
            current_values[symbol] = current_value

        last_rebalance = await self._get_last_regime_rebalance_time(symbols)
        tail_cohorts = self._load_tail_cohorts()
        owned_tail_hedge_con_ids = {cohort.con_id for cohort in tail_cohorts}

        weight_base = regime_rebalance.weight_base
        regime_margin_usage = self._resolve_regime_margin_usage()
        if weight_base == RegimeRebalanceBaseEnum.managed_stocks:
            total_value = sum(current_values.values())
        elif weight_base == RegimeRebalanceBaseEnum.net_liq_ex_options:
            excluded_value = 0.0
            # Regime checks receive the combined account map for tail ownership.
            # Ignore unrelated financing options, but retain state-owned tail puts.
            for positions in portfolio_positions.values():
                for position in positions:
                    if isinstance(position.contract, Option) and (
                        position.contract.symbol in configured_regime_symbols
                        or position.contract.conId in owned_tail_hedge_con_ids
                    ):
                        market_value = float(position.marketValue or 0.0)
                        excluded_value += market_value
            net_liq = float(account_summary["NetLiquidation"].value)
            adjusted_net_liq = net_liq - excluded_value
            total_value = math.floor(adjusted_net_liq * regime_margin_usage)
            log.notice(
                "Regime rebalancing base: mode=net_liq_ex_options "
                f"net_liq={dfmt(net_liq)} excluded_options={dfmt(excluded_value)} "
                f"margin_usage={ffmt(regime_margin_usage)} "
                f"base={dfmt(total_value)}"
            )
        else:
            net_liq = float(account_summary["NetLiquidation"].value)
            excluded_tail_hedge_value = self._tail_hedge_market_value(
                portfolio_positions,
                tail_cohorts,
            )
            adjusted_net_liq = net_liq - excluded_tail_hedge_value
            total_value = math.floor(adjusted_net_liq * regime_margin_usage)
            log.notice(
                "Regime rebalancing base: mode=net_liq "
                f"net_liq={dfmt(net_liq)} "
                f"excluded_tail_hedges={dfmt(excluded_tail_hedge_value)} "
                f"margin_usage={ffmt(regime_margin_usage)} "
                f"base={dfmt(total_value)}"
            )
        if total_value <= 0:
            log.error("Rebalance base value is not positive, skipping rebalancing.")
            raise ValueError("Regime-aware rebalancing requires a positive base value.")

        history_cache = RegimeHistoryCache(self._get_regime_aligned_closes)
        current_weights: Dict[str, float] = {}
        effective_weights, volatility_details = await self._resolve_effective_weights(
            symbols,
            symbol_configs,
            history_cache,
        )
        harvest_risk_ready_symbols = {
            symbol
            for symbol in symbols
            if not bool(
                getattr(
                    getattr(symbol_configs[symbol], "volatility_weight", None),
                    "enabled",
                    False,
                )
            )
            or symbol in volatility_details
        }
        total_effective_weight = sum(effective_weights.values())
        if total_effective_weight <= 0:
            log.error("Regime-aware rebalancing effective weights sum to zero.")
            raise ValueError(
                "Regime-aware rebalancing requires positive effective weights."
            )
        if total_effective_weight > 1.0 + regime_rebalance.eps:
            log.error(
                "Regime-aware rebalancing effective weights exceed 100%: "
                f"{pfmt(total_effective_weight)}."
            )
            raise ValueError(
                "Regime-aware rebalancing effective weights must not exceed 100%."
            )
        if weight_base == RegimeRebalanceBaseEnum.managed_stocks and not math.isclose(
            total_effective_weight,
            1.0,
            rel_tol=0.0,
            abs_tol=regime_rebalance.eps,
        ):
            log.error(
                "Regime-aware rebalancing managed_stocks weights must sum to 100%."
            )
            raise ValueError(
                "Regime-aware rebalancing requires effective weights to sum to "
                "100% when weight_base is managed_stocks."
            )
        unallocated_target_weight = max(0.0, 1.0 - total_effective_weight)
        if unallocated_target_weight > regime_rebalance.eps:
            log.notice(
                "Regime-aware rebalancing leaving "
                f"{pfmt(unallocated_target_weight)} outside managed targets for "
                "cash reserves."
            )
        normalized_effective_weights = {
            symbol: weight / total_effective_weight
            for symbol, weight in effective_weights.items()
        }
        for symbol in symbols:
            market_price = market_prices[symbol]
            current_position = current_positions[symbol]
            current_value = current_values[symbol]
            current_weights[symbol] = current_value / total_value
            target_weight = effective_weights[symbol]
            target_values[symbol] = target_weight * total_value
            target_shares[symbol] = math.floor(target_values[symbol] / market_price)
            share_gaps[symbol] = target_shares[symbol] - current_position
            relative_ratio = current_weights[symbol] / target_weight
            relative_ratios[symbol] = relative_ratio
            relative_drifts[symbol] = abs(relative_ratio - 1.0)

        invested_value = sum(current_values.values())
        proxy_symbols = [symbol for symbol in symbols if current_values[symbol] > 0]
        proxy_weights: Dict[str, float] = {}
        if proxy_symbols:
            proxy_invested = sum(current_values[symbol] for symbol in proxy_symbols)
            proxy_weights = {
                symbol: current_values[symbol] / proxy_invested
                for symbol in proxy_symbols
            }
        else:
            log.warning(
                "Regime proxy has no invested symbols; falling back to target weights."
            )
            proxy_weights = {symbol: effective_weights[symbol] for symbol in symbols}

        dates, values = await self._get_regime_proxy_series(
            symbols,
            regime_rebalance.lookback_days,
            regime_rebalance.cooldown_days,
            weights_override=proxy_weights,
            history_cache=history_cache,
        )
        if len(values) < regime_rebalance.lookback_days + 1:
            log.error("Insufficient historical data for regime rebalancing, aborting.")
            raise ValueError("Regime-aware rebalancing requires full lookback history.")

        window = np.array(values[-(regime_rebalance.lookback_days + 1) :])
        safe_prev = np.maximum(window[:-1], regime_rebalance.eps)
        safe_curr = np.maximum(window[1:], regime_rebalance.eps)
        r = np.log(safe_curr / safe_prev)
        sigma = math.sqrt(float(np.sum(r * r)))
        disp = abs(float(np.sum(r)))
        choppiness = sigma / max(disp, regime_rebalance.eps)
        chop_ok = choppiness >= regime_rebalance.choppiness_min

        diffs = np.abs(np.diff(window))
        efficiency = abs(float(window[-1] - window[0])) / max(
            float(np.sum(diffs)), regime_rebalance.eps
        )
        er_ok = efficiency <= regime_rebalance.efficiency_max
        regime_ok = chop_ok and er_ok

        ratio_gate = getattr(regime_rebalance, "ratio_gate", None)
        ratio_result: Optional[RatioGateResult] = None
        if ratio_gate is not None:
            _, ratio_aligned_closes = await history_cache.get(
                symbols,
                regime_rebalance.lookback_days,
                regime_rebalance.cooldown_days,
            )
            ratio_result = self._calculate_ratio_gate(
                symbols=symbols,
                dates=dates,
                aligned_closes=ratio_aligned_closes,
                ratio_gate=ratio_gate,
                effective_weights=effective_weights,
                lookback_days=regime_rebalance.lookback_days,
                eps=regime_rebalance.eps,
            )

        cooldown_ok = True
        if last_rebalance and regime_rebalance.cooldown_days > 0:
            cooldown_ok = self._cooldown_elapsed(
                last_rebalance, regime_rebalance.cooldown_days
            )

        soft_breach = any(
            drift + regime_rebalance.eps >= regime_rebalance.soft_band
            for drift in relative_drifts.values()
        )
        hard_breach = any(
            drift + regime_rebalance.eps >= regime_rebalance.hard_band
            for drift in relative_drifts.values()
        )
        hard_underweight_symbols = {
            symbol
            for symbol in symbols
            if current_weights[symbol] < effective_weights[symbol]
            and relative_drifts[symbol] + regime_rebalance.eps
            >= regime_rebalance.hard_band
        }

        max_relative_drift = max(relative_drifts.values()) if relative_drifts else 0.0
        hard_rebalance = hard_breach
        ratio_enabled = (
            bool(getattr(ratio_gate, "enabled", False)) if ratio_gate else False
        )
        ratio_gate_ok = (
            True
            if ratio_gate is None or not ratio_enabled
            else bool(ratio_result and ratio_result.ok)
        )
        shared_rebalance_gates_ok = regime_ok and cooldown_ok and ratio_gate_ok
        shared_rebalance_gate_blockers = []
        if not regime_ok:
            shared_rebalance_gate_blockers.append("regime")
        if not cooldown_ok:
            shared_rebalance_gate_blockers.append("cooldown")
        if not ratio_gate_ok:
            shared_rebalance_gate_blockers.append("ratio")
        soft_rebalance = soft_breach and shared_rebalance_gates_ok
        rebalance_fraction = 1.0
        if hard_rebalance:
            rebalance_fraction = regime_rebalance.hard_band_rebalance_fraction

        share_tolerance = 1
        flow_was_active = False
        deficit_was_active = False
        if self.data_store:
            state = self.data_store.get_last_event_payload("regime_rebalance_state")
            if state:
                flow_was_active = bool(state.get("flow_active", False))
                deficit_was_active = bool(state.get("deficit_active", False))

        unallocated_rebalance_capacity = total_value - invested_value
        flow_classification = (
            "inferred_capacity_deployment"
            if unallocated_rebalance_capacity > 0
            else "inferred_capacity_reduction"
            if unallocated_rebalance_capacity < 0
            else "none"
        )
        flow_trade_min_amount = total_value * regime_rebalance.flow_trade_min
        flow_trade_stop_amount = total_value * regime_rebalance.flow_trade_stop
        deficit_rail_start_amount = total_value * regime_rebalance.deficit_rail_start
        deficit_rail_stop_amount = total_value * regime_rebalance.deficit_rail_stop
        flow_gate = False
        deficit_gate = False
        if unallocated_rebalance_capacity < 0:
            deficit_amount = -unallocated_rebalance_capacity
            deficit_gate = deficit_amount >= deficit_rail_start_amount or (
                deficit_was_active and deficit_amount >= deficit_rail_stop_amount
            )
            if not deficit_gate:
                flow_gate = deficit_amount >= flow_trade_min_amount or (
                    flow_was_active and deficit_amount >= flow_trade_stop_amount
                )
        else:
            flow_gate = unallocated_rebalance_capacity >= flow_trade_min_amount or (
                flow_was_active
                and unallocated_rebalance_capacity >= flow_trade_stop_amount
            )

        flow_eligibility_gate_blockers = []
        if flow_classification != "inferred_capacity_deployment" and not regime_ok:
            flow_eligibility_gate_blockers.append("regime")
        if not cooldown_ok:
            flow_eligibility_gate_blockers.append("cooldown")
        if not ratio_gate_ok:
            flow_eligibility_gate_blockers.append("ratio")

        allowed_symbols = {
            symbol for symbol in symbols if self.config.trading_is_allowed(symbol)
        }
        flow_candidate_symbols = [
            symbol
            for symbol in symbols
            if symbol in allowed_symbols and abs(share_gaps[symbol]) > share_tolerance
        ]
        flow_net_share_gap = sum(
            share_gaps[symbol] for symbol in flow_candidate_symbols
        )
        flow_total_absolute_share_gap = sum(
            abs(share_gaps[symbol]) for symbol in flow_candidate_symbols
        )
        flow_value_gaps = {
            symbol: share_gaps[symbol] * market_prices[symbol]
            for symbol in flow_candidate_symbols
        }
        flow_net_value_gap = sum(flow_value_gaps.values())
        flow_total_absolute_value_gap = sum(
            abs(value) for value in flow_value_gaps.values()
        )
        flow_imbalance_ratio = (
            flow_net_value_gap / flow_total_absolute_value_gap
            if flow_total_absolute_value_gap > 0
            else None
        )

        def directional_flow_is_allowed(amount: float) -> bool:
            if flow_total_absolute_value_gap <= 0:
                return False
            if amount > 0:
                return (
                    flow_net_value_gap
                    > regime_rebalance.flow_imbalance_tau
                    * flow_total_absolute_value_gap
                )
            if amount < 0:
                return (
                    flow_net_value_gap
                    < -regime_rebalance.flow_imbalance_tau
                    * flow_total_absolute_value_gap
                )
            return False

        flow_directional_imbalance_ok = directional_flow_is_allowed(
            unallocated_rebalance_capacity
        )
        if flow_gate and not flow_directional_imbalance_ok:
            flow_eligibility_gate_blockers.append("directional_imbalance")
        flow_eligibility_gates_ok = not flow_eligibility_gate_blockers
        flow_rebalance_eligible = flow_gate and flow_eligibility_gates_ok

        def build_flow_orders(amount: float) -> Dict[str, int]:
            if amount == 0:
                return {}
            if not flow_candidate_symbols:
                return {}
            if flow_total_absolute_value_gap <= 0:
                return {}
            if not directional_flow_is_allowed(amount):
                return {}

            orders: Dict[str, int] = {}
            if amount > 0:
                deficits = {
                    symbol: max(share_gaps[symbol], 0)
                    for symbol in flow_candidate_symbols
                }
                total_deficit_value = sum(
                    deficit * market_prices[symbol]
                    for symbol, deficit in deficits.items()
                )
                if total_deficit_value <= 0:
                    return {}
                deployment_fraction = min(amount / total_deficit_value, 1.0)
                for symbol in flow_candidate_symbols:
                    deficit = deficits[symbol]
                    if deficit <= 0:
                        continue
                    if not self.config.trading_is_allowed(symbol):
                        continue
                    buy_shares = math.floor(deficit * deployment_fraction)
                    if buy_shares > 0:
                        orders[symbol] = buy_shares
            else:
                need = -amount
                excess_values = {
                    symbol: max(-flow_value_gaps[symbol], 0.0)
                    for symbol in flow_candidate_symbols
                }
                total_excess_value = sum(excess_values.values())
                if total_excess_value <= 0:
                    return {}
                for symbol in flow_candidate_symbols:
                    excess_value = excess_values[symbol]
                    if excess_value <= 0:
                        continue
                    if not self.config.trading_is_allowed(symbol):
                        continue
                    max_sell = max(
                        current_positions[symbol]
                        - max(target_shares[symbol] - share_tolerance, 0),
                        0,
                    )
                    if max_sell <= 0:
                        continue
                    alloc = need * (excess_value / total_excess_value)
                    sell_shares = min(
                        math.ceil(alloc / market_prices[symbol]), max_sell
                    )
                    if sell_shares > 0:
                        orders[symbol] = -sell_shares
            return orders

        def build_deficit_orders(
            shares_state: Dict[str, int],
            amount: float,
            allow_below_target: bool,
            allowed_symbols: set[str],
        ) -> Dict[str, int]:
            if amount <= 0:
                return {}
            orders: Dict[str, int] = {}
            initial_amount = amount

            overweight_symbols = [
                symbol
                for symbol in symbols
                if shares_state[symbol] > target_shares[symbol] + share_tolerance
                and symbol in allowed_symbols
            ]
            if overweight_symbols:
                overage_values = {
                    symbol: (
                        max(
                            shares_state[symbol]
                            - (target_shares[symbol] + share_tolerance),
                            0,
                        )
                        * market_prices[symbol]
                    )
                    for symbol in overweight_symbols
                }
                total_overage_value = sum(overage_values.values())
                for symbol in overweight_symbols:
                    overage_value = overage_values[symbol]
                    if overage_value <= 0:
                        continue
                    max_sell = max(
                        shares_state[symbol]
                        - max(target_shares[symbol] - share_tolerance, 0),
                        0,
                    )
                    if max_sell <= 0:
                        continue
                    alloc = (
                        initial_amount * (overage_value / total_overage_value)
                        if total_overage_value > 0
                        else amount
                    )
                    alloc = min(alloc, amount)
                    sell_shares = min(
                        math.ceil(alloc / market_prices[symbol]), max_sell
                    )
                    if sell_shares > 0:
                        orders[symbol] = orders.get(symbol, 0) - sell_shares
                        amount -= sell_shares * market_prices[symbol]
                        if amount <= 0:
                            return orders

            if not allow_below_target:
                return orders

            while amount > 0:
                any_sold = False
                for symbol in symbols:
                    if effective_weights[symbol] <= 0:
                        continue
                    if symbol not in allowed_symbols:
                        continue
                    max_sell = shares_state[symbol] + orders.get(symbol, 0)
                    if max_sell <= 0:
                        continue
                    alloc = amount * normalized_effective_weights[symbol]
                    sell_shares = min(
                        math.ceil(alloc / market_prices[symbol]), max_sell
                    )
                    if sell_shares <= 0:
                        continue
                    orders[symbol] = orders.get(symbol, 0) - sell_shares
                    amount -= sell_shares * market_prices[symbol]
                    any_sold = True
                    if amount <= 0:
                        break
                if not any_sold:
                    break
            return orders

        orders_by_symbol: Dict[str, int] = {}
        rebalance_mode = "no"
        deficit_gate_after = False
        if hard_rebalance or soft_rebalance:
            rebalance_mode = "hard" if hard_rebalance else "soft"
            for symbol in symbols:
                desired = target_shares[symbol] - current_positions[symbol]
                if hard_rebalance and not math.isclose(rebalance_fraction, 1.0):
                    desired = int(round(desired * rebalance_fraction))
                if desired == 0:
                    continue
                if symbol in allowed_symbols:
                    orders_by_symbol[symbol] = orders_by_symbol.get(symbol, 0) + desired

            shares_after = {
                symbol: current_positions[symbol] + orders_by_symbol.get(symbol, 0)
                for symbol in symbols
            }
            invested_after = sum(
                shares_after[symbol] * market_prices[symbol] for symbol in symbols
            )
            excess_after = total_value - invested_after
            deficit_amount_after = max(0.0, -excess_after)
            deficit_gate_after = deficit_amount_after >= deficit_rail_stop_amount
            if deficit_gate_after:
                deficit_needed = max(
                    0.0, deficit_amount_after - deficit_rail_stop_amount
                )
                deficit_orders = build_deficit_orders(
                    shares_after,
                    deficit_needed,
                    allow_below_target=True,
                    allowed_symbols=allowed_symbols,
                )
                if deficit_orders:
                    rebalance_mode = f"{rebalance_mode}+deficit"
                    for symbol, delta in deficit_orders.items():
                        if delta == 0:
                            continue
                        orders_by_symbol[symbol] = (
                            orders_by_symbol.get(symbol, 0) + delta
                        )
        elif deficit_gate:
            rebalance_mode = "deficit"
            deficit_needed = max(
                0.0,
                -unallocated_rebalance_capacity - deficit_rail_stop_amount,
            )
            deficit_orders = build_deficit_orders(
                current_positions,
                deficit_needed,
                allow_below_target=True,
                allowed_symbols=allowed_symbols,
            )
            if deficit_orders:
                for symbol, delta in deficit_orders.items():
                    if delta == 0:
                        continue
                    orders_by_symbol[symbol] = orders_by_symbol.get(symbol, 0) + delta
        elif flow_rebalance_eligible:
            rebalance_mode = "flow"
            flow_orders = build_flow_orders(unallocated_rebalance_capacity)
            for symbol, delta in flow_orders.items():
                if delta == 0:
                    continue
                orders_by_symbol[symbol] = orders_by_symbol.get(symbol, 0) + delta

        if deficit_gate:
            flow_decision_status = "superseded_by_deficit_rail"
        elif not flow_gate:
            flow_decision_status = "below_activation_threshold"
        elif not flow_directional_imbalance_ok:
            flow_decision_status = "blocked_by_directional_imbalance"
        elif flow_eligibility_gate_blockers:
            flow_decision_status = "blocked_by_shared_gates"
        elif rebalance_mode != "flow":
            flow_decision_status = f"superseded_by_{rebalance_mode}"
        else:
            flow_decision_status = "selected"
        flow_direction = (
            "buy"
            if unallocated_rebalance_capacity > 0
            else "sell"
            if unallocated_rebalance_capacity < 0
            else "flat"
        )
        deficit_active_next = (
            deficit_gate_after if (hard_rebalance or soft_rebalance) else deficit_gate
        )
        flow_active_next = flow_gate and rebalance_mode in {"flow", "no"}

        regime_summary: List[Dict[str, Any]] = []
        actionable_flow_buy_symbols: set[str] = set()
        net_liquidation_value = float(account_summary["NetLiquidation"].value)
        for symbol in symbols:
            target_weight = effective_weights[symbol]
            target_value = target_values[symbol]
            target_share = target_shares[symbol]
            trade_shares = orders_by_symbol.get(symbol, 0)
            filtered_trade_shares = trade_shares
            trading_allowed = self.config.trading_is_allowed(symbol)
            rebalance_policy_fn = getattr(self.config, "regime_rebalance_policy", None)
            rebalance_policy = (
                rebalance_policy_fn(symbol) if callable(rebalance_policy_fn) else None
            )
            allows_buy = (
                rebalance_policy.allows_buy() if rebalance_policy is not None else True
            )
            allows_sell = (
                rebalance_policy.allows_sell() if rebalance_policy is not None else True
            )
            mode_value = (
                rebalance_policy.mode.value if rebalance_policy is not None else "both"
            )
            min_shares = 1
            min_amount: Optional[float] = None
            min_percent_relative: Optional[float] = None

            if filtered_trade_shares > 0 and not allows_buy:
                filtered_trade_shares = 0
                action = f"[cyan]Skip (mode={mode_value})"
            elif filtered_trade_shares < 0 and not allows_sell:
                filtered_trade_shares = 0
                action = f"[cyan]Skip (mode={mode_value})"
            elif filtered_trade_shares != 0:
                trade_abs = abs(filtered_trade_shares)
                trade_amount = trade_abs * market_prices[symbol]
                symbol_config = symbol_configs[symbol]
                min_shares = (
                    self._as_int_or_none(
                        getattr(rebalance_policy, "min_threshold_shares", None)
                    )
                    or self._as_int_or_none(
                        getattr(symbol_config, "buy_only_min_threshold_shares", None)
                    )
                    or self._as_int_or_none(
                        getattr(symbol_config, "sell_only_min_threshold_shares", None)
                    )
                    or 1
                )
                min_amount = self._as_float_or_none(
                    getattr(rebalance_policy, "min_threshold_amount", None)
                )
                if min_amount is None:
                    min_amount = self._as_float_or_none(
                        getattr(symbol_config, "buy_only_min_threshold_amount", None)
                    )
                if min_amount is None:
                    min_amount = self._as_float_or_none(
                        getattr(symbol_config, "sell_only_min_threshold_amount", None)
                    )

                min_percent = self._as_float_or_none(
                    getattr(rebalance_policy, "min_threshold_percent", None)
                )
                if min_percent is None:
                    min_percent = self._as_float_or_none(
                        getattr(symbol_config, "buy_only_min_threshold_percent", None)
                    )
                if min_percent is None:
                    min_percent = self._as_float_or_none(
                        getattr(symbol_config, "sell_only_min_threshold_percent", None)
                    )

                min_percent_relative = self._as_float_or_none(
                    getattr(rebalance_policy, "min_threshold_percent_relative", None)
                )
                if min_percent_relative is None:
                    min_percent_relative = self._as_float_or_none(
                        getattr(
                            symbol_config,
                            "buy_only_min_threshold_percent_relative",
                            None,
                        )
                    )
                if min_percent_relative is None:
                    min_percent_relative = self._as_float_or_none(
                        getattr(
                            symbol_config,
                            "sell_only_min_threshold_percent_relative",
                            None,
                        )
                    )

                if min_percent is not None:
                    percent_min_amount = net_liquidation_value * min_percent
                    min_amount = (
                        max(min_amount, percent_min_amount)
                        if min_amount is not None
                        else percent_min_amount
                    )

                relative_diff = 0.0
                if target_value > 0:
                    if filtered_trade_shares > 0:
                        relative_diff = (
                            target_value - current_values[symbol]
                        ) / target_value
                    else:
                        relative_diff = (
                            current_values[symbol] - target_value
                        ) / target_value

                if trade_abs < min_shares:
                    filtered_trade_shares = 0
                    action = f"[yellow]Skip (below min shares {min_shares})"
                elif min_amount is not None and trade_amount < min_amount:
                    filtered_trade_shares = 0
                    action = (
                        f"[yellow]Skip (below min amount {dfmt(min_amount)}; "
                        f"would be {dfmt(trade_amount)})"
                    )
                elif (
                    min_percent_relative is not None
                    and target_value > 0
                    and relative_diff < min_percent_relative
                ):
                    filtered_trade_shares = 0
                    action = f"[yellow]Skip (below relative threshold {pfmt(min_percent_relative)})"

            if filtered_trade_shares != 0:
                if rebalance_mode == "flow" and filtered_trade_shares > 0:
                    actionable_flow_buy_symbols.add(symbol)
                to_trade.append(
                    (
                        symbol,
                        self.get_primary_exchange(symbol),
                        filtered_trade_shares,
                    )
                )
                action = (
                    f"[green]Buy {filtered_trade_shares}"
                    if filtered_trade_shares > 0
                    else f"[green]Sell {abs(filtered_trade_shares)}"
                )
            elif not trading_allowed:
                action = "[cyan]Skip (no_trading)"
            elif filtered_trade_shares == trade_shares:
                action = "[cyan]Hold"

            weight_delta = current_weights[symbol] - target_weight
            value_delta = current_values[symbol] - target_value
            shares_delta = current_positions[symbol] - target_share
            band_status = "hard" if hard_breach else "soft" if soft_breach else "no"
            gate_status = (
                f"mode={rebalance_mode} "
                f"band={band_status} "
                f"regime={'ok' if regime_ok else 'no'} "
                f"cooldown={'ok' if cooldown_ok else 'no'} "
                f"inferred_flow={flow_decision_status} "
                f"deficit={'on' if deficit_gate else 'off'}"
            )

            volatility_detail = volatility_details.get(symbol)
            target_weight_display = pfmt(target_weight)
            if volatility_detail is not None:
                target_weight_display = (
                    f"{pfmt(target_weight)} "
                    f"(base {pfmt(volatility_detail['base_weight'])}, "
                    f"vol {pfmt(volatility_detail['realized_vol'])})"
                )

            regime_summary.append(
                {
                    "symbol": symbol,
                    "market_price": market_prices[symbol],
                    "current_weight": current_weights[symbol],
                    "target_weight": target_weight,
                    "current_value": current_values[symbol],
                    "target_value": target_value,
                    "current_shares": current_positions[symbol],
                    "target_shares": target_share,
                    "shares_to_trade": filtered_trade_shares,
                    "weight_delta": weight_delta,
                    "value_delta": value_delta,
                    "shares_delta": shares_delta,
                    "trading_allowed": trading_allowed,
                    "minimum_trade_shares": min_shares,
                    "minimum_trade_amount": min_amount,
                    "minimum_trade_relative": min_percent_relative,
                    "action": action,
                    "target_weight_display": target_weight_display,
                    "gate_status": gate_status,
                    "volatility_weight": volatility_detail,
                }
            )

        to_trade = await self._apply_tail_harvest(
            orders=to_trade,
            account_summary=account_summary,
            portfolio_positions=portfolio_positions,
            market_prices=market_prices,
            regime_summary=regime_summary,
            hard_underweight_symbols=(
                hard_underweight_symbols & harvest_risk_ready_symbols
            ),
            cohorts=tail_cohorts,
        )
        for details in regime_summary:
            symbol = str(details["symbol"])
            table.add_row(
                symbol,
                f"{pfmt(details['current_weight'])}->"
                f"{details['target_weight_display']} "
                f"({pfmt(details['weight_delta'])})",
                f"{dfmt(details['current_value'])}->"
                f"{dfmt(details['target_value'])} "
                f"({dfmt(details['value_delta'])})",
                f"{ifmt(details['current_shares'])}->"
                f"{ifmt(details['target_shares'])} "
                f"({ifmt(details['shares_delta'])})",
                str(details["gate_status"]),
                str(details["action"]),
            )

        reserved_cash_for_post_management = 0.0
        if (
            rebalance_mode == "flow"
            and flow_gate
            and unallocated_rebalance_capacity > 0
        ):
            buy_order_value = sum(
                shares * market_prices[symbol]
                for symbol, _primary_exchange, shares in to_trade
                if shares > 0
            )
            if buy_order_value > 0:
                remaining_capacity = max(
                    0.0, unallocated_rebalance_capacity - buy_order_value
                )
                buy_orders_by_symbol = {
                    symbol: shares
                    for symbol, _primary_exchange, shares in to_trade
                    if shares > 0
                }
                remaining_target_gap_value = sum(
                    max(
                        target_shares[symbol]
                        - current_positions[symbol]
                        - buy_orders_by_symbol.get(symbol, 0),
                        0,
                    )
                    * market_prices[symbol]
                    for symbol in symbols
                    if symbol in actionable_flow_buy_symbols
                )
                reserved_cash_for_post_management = min(
                    remaining_capacity, remaining_target_gap_value
                )
            if reserved_cash_for_post_management > 0:
                log.notice(
                    "Regime rebalancing: reserving "
                    f"{dfmt(reserved_cash_for_post_management)} "
                    "from cash management for remaining inferred-capacity "
                    "target gaps."
                )
        self._reserve_cash_for_post_management(reserved_cash_for_post_management)

        ratio_gate_log = ""
        if ratio_gate is not None:
            ratio_gate_log = (
                " ratio_gate="
                + ("on" if ratio_enabled else "shadow")
                + (
                    ratio_result.to_log_fields()
                    if ratio_result is not None
                    else " ratio_ok=None"
                )
            )

        flow_telemetry = {
            "signal_kind": "inferred_unallocated_rebalance_capacity",
            "classification": flow_classification,
            "external_flow_detection": "not_performed",
            "capacity_source": "rebalance_base_minus_managed_sleeve_value",
            "weight_base": regime_rebalance.weight_base.value,
            "margin_usage": regime_margin_usage,
            "rebalance_base": total_value,
            "managed_sleeve_value": invested_value,
            "unallocated_rebalance_capacity": unallocated_rebalance_capacity,
            "direction": flow_direction,
            "gate": flow_gate,
            "decision_status": flow_decision_status,
            "shared_rebalance_gates_ok": shared_rebalance_gates_ok,
            "shared_gate_blockers": shared_rebalance_gate_blockers,
            "eligibility_gates_ok": flow_eligibility_gates_ok,
            "eligibility_gate_blockers": flow_eligibility_gate_blockers,
            "rebalance_eligible": flow_rebalance_eligible,
            "selected": rebalance_mode == "flow",
            "was_active": flow_was_active,
            "will_be_active": flow_active_next,
            "start_threshold": flow_trade_min_amount,
            "stop_threshold": flow_trade_stop_amount,
            "candidate_symbols": flow_candidate_symbols,
            "net_share_gap": flow_net_share_gap,
            "total_absolute_share_gap": flow_total_absolute_share_gap,
            "net_value_gap": flow_net_value_gap,
            "total_absolute_value_gap": flow_total_absolute_value_gap,
            "imbalance_unit": "dollars",
            "imbalance_ratio": flow_imbalance_ratio,
            "imbalance_tau": regime_rebalance.flow_imbalance_tau,
            "directional_imbalance_ok": flow_directional_imbalance_ok,
            "orders": to_trade if rebalance_mode == "flow" else [],
            "reserved_cash_for_post_management": reserved_cash_for_post_management,
        }
        deficit_telemetry = {
            "gate": deficit_gate,
            "was_active": deficit_was_active,
            "will_be_active": deficit_active_next,
            "start_threshold": deficit_rail_start_amount,
            "stop_threshold": deficit_rail_stop_amount,
        }

        log.info(
            f"Regime rebalancing gates: max_relative_drift={pfmt(max_relative_drift)} "
            f"soft_band={pfmt(regime_rebalance.soft_band, 0)} "
            f"hard_band={pfmt(regime_rebalance.hard_band, 0)} "
            f"hard_breach={hard_breach} soft_breach={soft_breach} "
            f"chop={ffmt(choppiness)} chop_ok={chop_ok} "
            f"er={pfmt(efficiency)} er_ok={er_ok} "
            f"regime_ok={regime_ok} cooldown_ok={cooldown_ok} "
            f"shared_gates_ok={shared_rebalance_gates_ok} "
            f"mode={rebalance_mode}" + ratio_gate_log
        )
        log.info(
            "Regime rebalancing inferred-capacity flow: "
            "signal=inferred_unallocated_rebalance_capacity "
            "external_flow_detection=not_performed "
            f"weight_base={regime_rebalance.weight_base.value} "
            f"margin_usage={ffmt(regime_margin_usage)} "
            f"rebalance_base={dfmt(total_value)} "
            f"managed_sleeves={dfmt(invested_value)} "
            f"unallocated_capacity={dfmt(unallocated_rebalance_capacity)} "
            f"classification={flow_classification} "
            f"direction={flow_direction} gate={flow_gate} "
            f"decision={flow_decision_status} "
            f"ordinary_gate_failures="
            f"{','.join(shared_rebalance_gate_blockers) or 'none'} "
            f"eligibility_gate_blockers="
            f"{','.join(flow_eligibility_gate_blockers) or 'none'} "
            f"eligibility_gates_ok={flow_eligibility_gates_ok} "
            f"eligible={flow_rebalance_eligible} "
            f"was_active={flow_was_active} will_be_active={flow_active_next} "
            f"start={pfmt(regime_rebalance.flow_trade_min)}"
            f"({dfmt(flow_trade_min_amount)}) "
            f"stop={pfmt(regime_rebalance.flow_trade_stop)}"
            f"({dfmt(flow_trade_stop_amount)}) "
            f"net_value_gap={dfmt(flow_net_value_gap)} "
            f"total_abs_value_gap={dfmt(flow_total_absolute_value_gap)} "
            f"imbalance_ratio={_ffmt_or_dash(flow_imbalance_ratio)} "
            f"imbalance_tau={ffmt(regime_rebalance.flow_imbalance_tau)} "
            f"directional_ok={flow_directional_imbalance_ok}"
        )
        log.info(
            "Regime rebalancing deficit rail: "
            f"gate={deficit_gate} was_active={deficit_was_active} "
            f"will_be_active={deficit_active_next} "
            f"start={pfmt(regime_rebalance.deficit_rail_start)}"
            f"({dfmt(deficit_rail_start_amount)}) "
            f"stop={pfmt(regime_rebalance.deficit_rail_stop)}"
            f"({dfmt(deficit_rail_stop_amount)})"
        )
        if self.data_store:
            ratio_payload = None
            if ratio_gate is not None:
                ratio_payload = (
                    ratio_result.to_payload(enabled=ratio_enabled)
                    if ratio_result is not None
                    else {"enabled": ratio_enabled}
                )
            self.data_store.record_event(
                "regime_rebalance_gate",
                {
                    "telemetry_schema_version": 3,
                    "legacy_field_aliases": {
                        "excess_cash": "unallocated_rebalance_capacity"
                    },
                    "symbols": symbols,
                    "total_effective_weight": total_effective_weight,
                    "unallocated_target_weight": unallocated_target_weight,
                    "max_relative_drift": max_relative_drift,
                    "soft_band": regime_rebalance.soft_band,
                    "hard_band": regime_rebalance.hard_band,
                    "hard_breach": hard_breach,
                    "soft_breach": soft_breach,
                    "choppiness": choppiness,
                    "choppiness_ok": chop_ok,
                    "efficiency": efficiency,
                    "efficiency_ok": er_ok,
                    "regime_ok": regime_ok,
                    "cooldown_ok": cooldown_ok,
                    "ratio_gate_ok": ratio_gate_ok,
                    "shared_rebalance_gates_ok": shared_rebalance_gates_ok,
                    "flow_gate": flow_gate,
                    "deficit_gate": deficit_gate,
                    "unallocated_rebalance_capacity": unallocated_rebalance_capacity,
                    # Retain the legacy key for historical telemetry queries.
                    "excess_cash": unallocated_rebalance_capacity,
                    "reserved_cash_for_post_management": (
                        reserved_cash_for_post_management
                    ),
                    "mode": rebalance_mode,
                    "orders": to_trade,
                    "ratio_gate": ratio_payload,
                    "flow": flow_telemetry,
                    "deficit": deficit_telemetry,
                },
            )
            self.data_store.record_event(
                "regime_rebalance_summary",
                {
                    "telemetry_schema_version": 3,
                    "legacy_field_aliases": {
                        "excess_cash": "unallocated_rebalance_capacity"
                    },
                    "symbols": symbols,
                    "total_value": total_value,
                    "total_effective_weight": total_effective_weight,
                    "unallocated_target_weight": unallocated_target_weight,
                    "hard_breach": hard_breach,
                    "soft_breach": soft_breach,
                    "regime_ok": regime_ok,
                    "cooldown_ok": cooldown_ok,
                    "flow_gate": flow_gate,
                    "deficit_gate": deficit_gate,
                    "unallocated_rebalance_capacity": unallocated_rebalance_capacity,
                    # Retain the legacy key for historical telemetry queries.
                    "excess_cash": unallocated_rebalance_capacity,
                    "reserved_cash_for_post_management": (
                        reserved_cash_for_post_management
                    ),
                    "mode": rebalance_mode,
                    "summary": regime_summary,
                    "ratio_gate": ratio_payload,
                    "flow": flow_telemetry,
                    "deficit": deficit_telemetry,
                },
            )
            self.data_store.record_event(
                "regime_rebalance_state",
                {
                    "flow_active": flow_active_next,
                    "deficit_active": deficit_active_next,
                },
            )
            if volatility_details:
                self.data_store.record_event(
                    "volatility_weight_state",
                    {
                        "total_effective_weight": total_effective_weight,
                        "unallocated_target_weight": unallocated_target_weight,
                        "symbols": {
                            symbol: {
                                "base_weight": details["base_weight"],
                                "effective_weight": details["effective_weight"],
                                "target_weight": details["target_weight"],
                                "realized_vol": details["realized_vol"],
                                "smoothing_factor": details["smoothing_factor"],
                            }
                            for symbol, details in volatility_details.items()
                        },
                    },
                )

        return (table, to_trade)
