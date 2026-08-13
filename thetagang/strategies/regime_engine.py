from __future__ import annotations

import asyncio
import math
import uuid
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
from thetagang.strategies.runtime_services import resolve_symbol_configs
from thetagang.strategies.tail_hedge_engine import (
    TAIL_HEDGE_ACTIVE_HARVEST_STATUSES,
    TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX,
    TAIL_HEDGE_STATE_EVENT,
    TAIL_HEDGE_STATE_SCHEMA_VERSION,
    TAIL_HEDGE_STATE_STRATEGY,
    active_tail_harvest_con_ids,
    active_tail_harvest_symbols,
    tail_hedge_owned_con_ids,
    tail_hedge_state_collections,
)
from thetagang.trading_operations import OrderOperations
from thetagang.util import midpoint_or_market_price

AlignedClosesResult = Tuple[List[date], Dict[str, List[float]]]
AlignedClosesFetcher = Callable[
    [List[str], int, int], Coroutine[Any, Any, AlignedClosesResult]
]
ClosesBySymbol = Dict[str, Dict[date, float]]
TRADING_DAYS_PER_YEAR = 252
REGIME_HISTORY_TIMEFRAME = "1 day"
REGIME_HISTORY_MAX_ATTEMPTS = 3
REGIME_HISTORY_RETRY_DELAY_SECONDS = 0.25


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
        get_buying_power: Callable[[Dict[str, AccountValue]], int],
        now_provider: Callable[[], datetime],
        set_reserved_cash_for_post_management: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.order_ops = order_ops
        self.data_store = data_store
        self._get_primary_exchange = get_primary_exchange
        self._get_buying_power = get_buying_power
        self._now = now_provider
        self._set_reserved_cash_for_post_management = (
            set_reserved_cash_for_post_management
        )
        self.regime_rebalance_order_ref_prefix = "tg:regime-rebalance"
        self._active_harvest_symbols: set[str] = set()
        self._recent_execution_fills: list[Any] = []
        self._pending_harvest_stock_buys: dict[str, dict[str, Any]] = {}
        self._flow_reserved_cash = 0.0
        self._approved_buy_symbols: set[str] = set()

    def approved_buy_symbols(self) -> set[str]:
        return set(self._approved_buy_symbols)

    def _reserve_cash_for_post_management(self, amount: float) -> None:
        if self._set_reserved_cash_for_post_management is None:
            return
        self._set_reserved_cash_for_post_management(max(0.0, amount))

    def _tail_harvesting_enabled(self) -> bool:
        return bool(
            self.data_store is not None and self.config.strategies.tail_hedge.enabled
        )

    def _load_tail_state(
        self,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        if self.data_store is None:
            return [], [], []
        state = self.data_store.get_last_event_payload(
            TAIL_HEDGE_STATE_EVENT,
            raise_on_error=True,
        )
        tranches, entry_history, harvest_plans = tail_hedge_state_collections(
            state,
            account_number=self.config.runtime.account.number,
        )
        return tranches, entry_history, harvest_plans

    def _load_reconciled_tail_state(
        self,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        tranches, entry_history, harvest_plans = self._load_tail_state()
        if not tranches and not harvest_plans:
            return tranches, entry_history, harvest_plans

        self._supplement_harvest_fills_from_state(harvest_plans)
        if self._reconcile_harvest_plans(harvest_plans):
            self._record_tail_state(
                (
                    "proceeds_realized"
                    if any(
                        plan.get("status") == "proceeds_realized"
                        for plan in harvest_plans
                    )
                    else "harvest_reconciled"
                ),
                tranches=tranches,
                entry_history=entry_history,
                harvest_plans=harvest_plans,
            )
        return tranches, entry_history, harvest_plans

    @staticmethod
    def _active_harvest_credit(harvest_plans: list[dict[str, Any]]) -> float:
        credit = 0.0
        for plan in harvest_plans:
            status = plan.get("status")
            if status == "put_sell_working":
                credit += float(plan.get("actual_put_sale_proceeds", 0.0) or 0.0)
            elif status in {
                "proceeds_realized",
                "rebalance_credit_ready",
                "stock_buy_enqueued",
            }:
                credit += float(plan.get("remaining_rebalance_credit", 0.0) or 0.0)
        return round(max(0.0, credit), 2)

    def _cash_fund_value(
        self,
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> float:
        cash_management = getattr(self.config.strategies, "cash_management", None)
        if not bool(getattr(cash_management, "enabled", False)):
            return 0.0
        cash_fund = getattr(cash_management, "cash_fund", None)
        if not isinstance(cash_fund, str) or not cash_fund:
            return 0.0

        value = 0.0
        for positions in portfolio_positions.values():
            for position in positions:
                if (
                    not isinstance(position.contract, Stock)
                    or position.contract.symbol != cash_fund
                    or float(position.position) <= 0
                ):
                    continue
                market_value = float(getattr(position, "marketValue", 0.0) or 0.0)
                if market_value <= 0:
                    market_price = float(getattr(position, "marketPrice", 0.0) or 0.0)
                    market_value = max(0.0, float(position.position) * market_price)
                value += max(0.0, market_value)
        return value

    def _queued_cash_debits(self) -> float:
        debit = 0.0
        for contract, order, _intent_id in self.order_ops.orders.records():
            if str(getattr(order, "action", "")).upper() != "BUY":
                continue
            price = float(getattr(order, "lmtPrice", 0.0) or 0.0)
            quantity = float(getattr(order, "totalQuantity", 0.0) or 0.0)
            multiplier = (
                1.0
                if isinstance(contract, Stock)
                else float(getattr(contract, "multiplier", 0.0) or 100.0)
            )
            debit += max(0.0, price * quantity * multiplier)
        return debit

    def _ordinary_rebalance_funding(
        self,
        *,
        orders: List[Tuple[str, str, int]],
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
        market_prices: Dict[str, float],
        harvest_plans: list[dict[str, Any]],
    ) -> dict[str, float]:
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
        required_cash = max(0.0, approved_buys - approved_sells)

        cash_value = account_summary.get("TotalCashValue")
        available_cash = float(getattr(cash_value, "value", 0.0) or 0.0)
        cash_management = getattr(self.config.strategies, "cash_management", None)
        cash_target = (
            float(getattr(cash_management, "target_cash_balance", 0.0) or 0.0)
            if bool(getattr(cash_management, "enabled", False))
            else 0.0
        )
        cash_fund_value = self._cash_fund_value(portfolio_positions)
        active_credit = self._active_harvest_credit(harvest_plans)
        queued_cash_debits = self._queued_cash_debits()
        existing_reservations = active_credit + queued_cash_debits
        ordinary_liquidity = max(
            0.0,
            available_cash + cash_fund_value - cash_target - existing_reservations,
        )
        funding_shortfall = max(0.0, required_cash - ordinary_liquidity)
        return {
            "approved_buys": round(approved_buys, 2),
            "approved_sells": round(approved_sells, 2),
            "required_cash": round(required_cash, 2),
            "available_cash": round(available_cash, 2),
            "cash_fund_value": round(cash_fund_value, 2),
            "cash_target": round(cash_target, 2),
            "active_harvest_credit": round(active_credit, 2),
            "queued_cash_debits": round(queued_cash_debits, 2),
            "existing_reservations": round(existing_reservations, 2),
            "ordinary_liquidity": round(ordinary_liquidity, 2),
            "funding_shortfall": round(funding_shortfall, 2),
        }

    def _tail_hedge_market_value(
        self,
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> float:
        if self.data_store is None:
            return 0.0
        state = self.data_store.get_last_event_payload(
            TAIL_HEDGE_STATE_EVENT,
            raise_on_error=True,
        )
        owned_con_ids = tail_hedge_owned_con_ids(
            state,
            account_number=self.config.runtime.account.number,
        )
        return sum(
            float(position.marketValue or 0.0)
            for positions in portfolio_positions.values()
            for position in positions
            if isinstance(position.contract, Option)
            and position.contract.conId in owned_con_ids
        )

    def _record_tail_state(
        self,
        status: str,
        *,
        tranches: list[dict[str, Any]],
        entry_history: list[dict[str, Any]],
        harvest_plans: list[dict[str, Any]],
        **payload: Any,
    ) -> None:
        if self.data_store is None:
            raise RuntimeError("Tail harvesting requires SQLite state storage")
        state = {
            "schema_version": TAIL_HEDGE_STATE_SCHEMA_VERSION,
            "strategy": TAIL_HEDGE_STATE_STRATEGY,
            "account": self.config.runtime.account.number,
            "status": status,
            "state_recorded_at": self._now(),
            "tranches": tranches,
            "entry_history": entry_history,
            "harvest_plans": harvest_plans,
            **payload,
        }
        if not self.data_store.record_event(TAIL_HEDGE_STATE_EVENT, state):
            raise RuntimeError("Failed to persist required tail-harvest state")

    @staticmethod
    def _fill_order_ref(fill: Any) -> Optional[str]:
        order_ref = getattr(getattr(fill, "execution", None), "orderRef", None)
        return order_ref if isinstance(order_ref, str) and order_ref else None

    @staticmethod
    def _fill_side(fill: Any) -> str:
        return str(getattr(getattr(fill, "execution", None), "side", "")).upper()

    @staticmethod
    def _fill_quantity(fill: Any) -> float:
        return max(
            0.0,
            float(getattr(getattr(fill, "execution", None), "shares", 0.0) or 0.0),
        )

    @staticmethod
    def _fill_price(fill: Any) -> float:
        return max(
            0.0,
            float(getattr(getattr(fill, "execution", None), "price", 0.0) or 0.0),
        )

    @staticmethod
    def _fill_commission(fill: Any) -> float:
        raw_commission = getattr(
            getattr(fill, "commissionReport", None), "commission", 0.0
        )
        if not isinstance(raw_commission, (int, float)) or isinstance(
            raw_commission, bool
        ):
            return 0.0
        commission = float(raw_commission)
        return max(0.0, commission) if math.isfinite(commission) else 0.0

    def _account_open_trade_refs(self) -> set[str]:
        account_number = self.config.runtime.account.number
        open_trades = self.ibkr.open_trades()
        if not isinstance(open_trades, list):
            return set()
        return {
            order_ref
            for trade in open_trades
            if getattr(getattr(trade, "order", None), "account", None) == account_number
            and isinstance(
                order_ref := getattr(getattr(trade, "order", None), "orderRef", None),
                str,
            )
            and order_ref
        }

    def _account_working_option_con_ids(self) -> set[int]:
        account_number = self.config.runtime.account.number
        open_trades = self.ibkr.open_trades()
        if not isinstance(open_trades, list):
            return set()
        return {
            int(trade.contract.conId)
            for trade in open_trades
            if getattr(getattr(trade, "order", None), "account", None) == account_number
            and isinstance(getattr(trade, "contract", None), Option)
            and type(getattr(trade.contract, "conId", None)) is int
            and trade.contract.conId > 0
        }

    def _fills_for_order_ref(self, order_ref: str, *, buy: bool) -> list[Any]:
        accepted_sides = {"BOT", "BUY"} if buy else {"SLD", "SELL"}
        return [
            fill
            for fill in self._recent_execution_fills
            if self._fill_order_ref(fill) == order_ref
            and self._fill_side(fill) in accepted_sides
        ]

    def _supplement_harvest_fills_from_state(
        self,
        harvest_plans: list[dict[str, Any]],
    ) -> None:
        if self.data_store is None:
            return
        order_refs = {
            str(order_ref)
            for plan in harvest_plans
            for order_ref in [
                *[
                    sale.get("order_ref")
                    for sale in plan.get("put_sales", [])
                    if isinstance(sale, dict)
                ],
                (
                    plan.get("stock_buy", {}).get("order_ref")
                    if isinstance(plan.get("stock_buy"), dict)
                    else None
                ),
            ]
            if isinstance(order_ref, str) and order_ref
        }
        stored_fills = self.data_store.get_executions_for_order_refs(order_refs)
        if not isinstance(stored_fills, list):
            return

        def fill_identity(fill: Any) -> tuple[Any, ...]:
            execution = getattr(fill, "execution", None)
            exec_id = getattr(execution, "execId", None)
            if exec_id:
                return ("exec_id", exec_id)
            return (
                "fill",
                getattr(execution, "orderRef", None),
                getattr(execution, "side", None),
                getattr(execution, "shares", None),
                getattr(execution, "price", None),
                getattr(fill, "time", None) or getattr(execution, "time", None),
            )

        known = {fill_identity(fill) for fill in self._recent_execution_fills}
        for fill in stored_fills:
            identity = fill_identity(fill)
            if identity in known:
                continue
            self._recent_execution_fills.append(fill)
            known.add(identity)

    def _reconcile_harvest_plans(
        self,
        harvest_plans: list[dict[str, Any]],
    ) -> bool:
        open_refs = self._account_open_trade_refs()
        changed = False
        for index, original in enumerate(harvest_plans):
            status = original.get("status")
            if status not in TAIL_HEDGE_ACTIVE_HARVEST_STATUSES:
                continue
            plan = dict(original)
            sales = [dict(sale) for sale in plan.get("put_sales", [])]

            if status == "put_sell_working":
                total_proceeds = 0.0
                total_gross_proceeds = 0.0
                total_commissions = 0.0
                all_filled = bool(sales)
                any_working = False
                any_filled = False
                for sale in sales:
                    order_ref = str(sale.get("order_ref", ""))
                    fills = self._fills_for_order_ref(order_ref, buy=False)
                    filled_quantity = sum(self._fill_quantity(fill) for fill in fills)
                    multiplier = float(sale.get("multiplier", 100.0) or 100.0)
                    gross_proceeds = sum(
                        self._fill_quantity(fill) * self._fill_price(fill) * multiplier
                        for fill in fills
                    )
                    commissions = sum(self._fill_commission(fill) for fill in fills)
                    realized_proceeds = max(0.0, gross_proceeds - commissions)
                    requested_quantity = int(sale.get("quantity", 0) or 0)
                    sale["filled_quantity"] = filled_quantity
                    sale["gross_proceeds"] = round(gross_proceeds, 2)
                    sale["commissions"] = round(commissions, 2)
                    sale["realized_proceeds"] = round(realized_proceeds, 2)
                    sale["order_status"] = (
                        "filled"
                        if filled_quantity + 1e-9 >= requested_quantity
                        else "working"
                        if order_ref in open_refs
                        else "not_working"
                    )
                    total_proceeds += realized_proceeds
                    total_gross_proceeds += gross_proceeds
                    total_commissions += commissions
                    all_filled = all_filled and (
                        filled_quantity + 1e-9 >= requested_quantity
                    )
                    any_working = any_working or order_ref in open_refs
                    any_filled = any_filled or filled_quantity > 0

                plan["put_sales"] = sales
                plan["gross_put_sale_proceeds"] = round(total_gross_proceeds, 2)
                plan["put_sale_commissions"] = round(total_commissions, 2)
                plan["actual_put_sale_proceeds"] = round(total_proceeds, 2)
                if all_filled or (any_filled and not any_working):
                    plan["status"] = "proceeds_realized"
                    plan["remaining_rebalance_credit"] = round(total_proceeds, 2)
                    plan["put_sales_filled_at"] = self._now()
                elif not any_working:
                    plan["status"] = "canceled"
                    plan["canceled_at"] = self._now()
                    plan["cancel_reason"] = "put_sale_not_filled"
                harvest_plans[index] = plan
                changed = True
                continue

            if status != "stock_buy_enqueued":
                continue
            stock_buy = dict(plan.get("stock_buy", {}))
            order_ref = str(stock_buy.get("order_ref", ""))
            fills = self._fills_for_order_ref(order_ref, buy=True)
            filled_quantity = sum(self._fill_quantity(fill) for fill in fills)
            gross_cost = sum(
                self._fill_quantity(fill) * self._fill_price(fill) for fill in fills
            )
            commissions = sum(self._fill_commission(fill) for fill in fills)
            actual_cost = gross_cost + commissions
            requested_quantity = int(stock_buy.get("quantity", 0) or 0)
            stock_buy["filled_quantity"] = filled_quantity
            stock_buy["gross_cost"] = round(gross_cost, 2)
            stock_buy["commissions"] = round(commissions, 2)
            stock_buy["actual_cost"] = round(actual_cost, 2)
            stock_buy["order_status"] = (
                "filled"
                if filled_quantity + 1e-9 >= requested_quantity
                else "working"
                if order_ref in open_refs
                else "not_working"
            )
            plan["stock_buy"] = stock_buy
            actual_proceeds = float(plan.get("actual_put_sale_proceeds", 0.0) or 0.0)
            plan["remaining_rebalance_credit"] = round(
                max(0.0, actual_proceeds - actual_cost), 2
            )
            if filled_quantity + 1e-9 >= requested_quantity or (
                filled_quantity > 0 and order_ref not in open_refs
            ):
                unused_proceeds = round(max(0.0, actual_proceeds - actual_cost), 2)
                plan["status"] = "completed"
                plan["completed_at"] = self._now()
                plan["remaining_rebalance_credit"] = unused_proceeds
                plan["unused_proceeds"] = unused_proceeds
            elif order_ref not in open_refs:
                plan["status"] = "canceled"
                plan["canceled_at"] = self._now()
                plan["cancel_reason"] = "stock_buy_not_filled"
            harvest_plans[index] = plan
            changed = True
        return changed

    @staticmethod
    def _position_map_by_con_id(
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> dict[int, PortfolioItem]:
        return {
            position.contract.conId: position
            for positions in portfolio_positions.values()
            for position in positions
            if isinstance(position.contract, Option)
            and position.contract.right.upper().startswith("P")
            and position.contract.conId > 0
            and float(position.position) > 0
        }

    def _select_profitable_put_sales(
        self,
        *,
        symbol: str,
        tranches: list[dict[str, Any]],
        harvest_plans: list[dict[str, Any]],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> list[dict[str, Any]]:
        positions = self._position_map_by_con_id(portfolio_positions)
        allocated_con_ids = active_tail_harvest_con_ids(harvest_plans)
        working_con_ids = self._account_working_option_con_ids()
        candidates: list[dict[str, Any]] = []
        for tranche in sorted(
            (
                item
                for item in tranches
                if item.get("symbol") == symbol and item.get("status") == "active"
            ),
            key=lambda item: str(item.get("expiration", "")),
        ):
            con_id = int(tranche["con_id"])
            if con_id in allocated_con_ids or con_id in working_con_ids:
                continue
            position = positions.get(con_id)
            if position is None or position.contract.symbol != symbol:
                continue
            available_quantity = min(
                int(math.floor(float(position.position))),
                int(tranche.get("quantity", 0) or 0),
            )
            if available_quantity <= 0:
                continue
            position_quantity = max(float(position.position), 1.0)
            market_value_per_contract = (
                float(getattr(position, "marketValue", 0.0) or 0.0) / position_quantity
            )
            average_cost = float(getattr(position, "averageCost", 0.0) or 0.0)
            if average_cost <= 0:
                tranche_quantity = max(int(tranche.get("quantity", 1) or 1), 1)
                average_cost = (
                    float(tranche.get("entry_cost", 0.0) or 0.0) / tranche_quantity
                )
            unrealized_pnl = getattr(position, "unrealizedPNL", None)
            profitable = (
                float(unrealized_pnl) > 0
                if isinstance(unrealized_pnl, (int, float))
                and not isinstance(unrealized_pnl, bool)
                else market_value_per_contract > average_cost
            )
            if not profitable or market_value_per_contract <= 0:
                continue
            multiplier = float(position.contract.multiplier or 100.0)
            candidates.append(
                {
                    "entry_id": tranche["entry_id"],
                    "con_id": con_id,
                    "local_symbol": getattr(position.contract, "localSymbol", ""),
                    "expiration": tranche["expiration"],
                    "available_quantity": available_quantity,
                    "estimated_proceeds_per_contract": round(
                        market_value_per_contract, 2
                    ),
                    "cost_basis_per_contract": round(average_cost, 2),
                    "multiplier": multiplier,
                }
            )

        for candidate in candidates:
            per_contract = float(candidate["estimated_proceeds_per_contract"])
            candidate["quantity"] = int(candidate.pop("available_quantity"))
            candidate["estimated_proceeds"] = round(
                per_contract * int(candidate["quantity"]), 2
            )
        return candidates

    async def _enqueue_harvest_sales(
        self,
        *,
        plan_index: int,
        tranches: list[dict[str, Any]],
        entry_history: list[dict[str, Any]],
        harvest_plans: list[dict[str, Any]],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> None:
        plan = dict(harvest_plans[plan_index])
        positions = self._position_map_by_con_id(portfolio_positions)
        prepared: list[tuple[Any, Any]] = []
        updated_sales: list[dict[str, Any]] = []
        remaining = float(plan.get("approved_buy_amount", 0.0) or 0.0)
        for sale_index, raw_sale in enumerate(plan.get("put_sales", [])):
            sale = dict(raw_sale)
            con_id = int(sale["con_id"])
            position = positions.get(con_id)
            if position is None:
                continue
            contract = position.contract
            contract.exchange = self.order_ops.get_order_exchange()
            ticker = await self.ibkr.get_ticker_for_contract(
                contract,
                required_fields=[],
                optional_fields=[TickerField.MIDPOINT, TickerField.MARKET_PRICE],
            )
            limit_price = round(max(float(midpoint_or_market_price(ticker)), 0.01), 2)
            multiplier = float(sale.get("multiplier", 100.0) or 100.0)
            if limit_price * multiplier <= float(
                sale.get("cost_basis_per_contract", 0.0) or 0.0
            ):
                continue
            order_ref = (
                f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:{plan['plan_id']}:{sale_index}"
            )
            per_contract_proceeds = limit_price * multiplier
            quantity = min(
                int(sale["quantity"]),
                max(1, math.ceil(remaining / per_contract_proceeds)),
            )
            order = self.order_ops.create_limit_order(
                action="SELL",
                quantity=quantity,
                limit_price=limit_price,
                use_default_algo=False,
                order_ref=order_ref,
                transmit=True,
            )
            sale.update(
                order_ref=order_ref,
                order_status="enqueued",
                sell_limit_price=limit_price,
                sell_enqueued_at=self._now(),
                quantity=quantity,
                estimated_proceeds_per_contract=round(per_contract_proceeds, 2),
                estimated_proceeds=round(per_contract_proceeds * quantity, 2),
            )
            updated_sales.append(sale)
            prepared.append((contract, order))
            remaining -= per_contract_proceeds * quantity
            if remaining <= 0:
                break

        if not prepared:
            plan["status"] = "canceled"
            plan["canceled_at"] = self._now()
            plan["cancel_reason"] = "profitable_put_no_longer_available"
            harvest_plans[plan_index] = plan
            self._record_tail_state(
                "harvest_canceled",
                tranches=tranches,
                entry_history=entry_history,
                harvest_plans=harvest_plans,
                action_plan_id=plan["plan_id"],
            )
            return

        plan["status"] = "put_sell_working"
        plan["put_sales"] = updated_sales
        plan["put_sell_enqueued_at"] = self._now()
        harvest_plans[plan_index] = plan
        self._record_tail_state(
            "put_sell_working",
            tranches=tranches,
            entry_history=entry_history,
            harvest_plans=harvest_plans,
            action_plan_id=plan["plan_id"],
        )
        for contract, order in prepared:
            self.order_ops.enqueue_order(contract, order)

    @staticmethod
    def _harvest_reserved_cash(harvest_plans: list[dict[str, Any]]) -> float:
        reserved = 0.0
        for plan in harvest_plans:
            status = plan.get("status")
            if status == "put_sell_working":
                reserved += sum(
                    float(sale.get("estimated_proceeds", 0.0) or 0.0)
                    for sale in plan.get("put_sales", [])
                    if isinstance(sale, dict)
                )
            elif status in {
                "proceeds_realized",
                "rebalance_credit_ready",
                "stock_buy_enqueued",
            }:
                reserved += float(plan.get("remaining_rebalance_credit", 0.0) or 0.0)
        return round(max(0.0, reserved), 2)

    async def _request_tail_harvest(
        self,
        *,
        symbol: str,
        approved_quantity: int,
        ordinary_approved_quantity: int,
        market_price: float,
        put_sales: list[dict[str, Any]],
        funding: dict[str, float],
        tranches: list[dict[str, Any]],
        entry_history: list[dict[str, Any]],
        harvest_plans: list[dict[str, Any]],
        portfolio_positions: Dict[str, List[PortfolioItem]],
        summary: dict[str, Any],
        rebalance_mode: str,
    ) -> bool:
        approved_buy_amount = round(approved_quantity * market_price, 2)
        plan_id = uuid.uuid4().hex[:12]
        plan = {
            "plan_id": plan_id,
            "symbol": symbol,
            "status": "harvest_requested",
            "requested_at": self._now(),
            "approved_buy_amount": approved_buy_amount,
            "target_snapshot": {
                "rebalance_mode": rebalance_mode,
                "market_price": market_price,
                "current_weight": summary.get("current_weight"),
                "target_weight": summary.get("target_weight"),
                "current_value": summary.get("current_value"),
                "target_value": summary.get("target_value"),
                "current_shares": summary.get("current_shares"),
                "target_shares": summary.get("target_shares"),
                "ordinary_approved_shares": ordinary_approved_quantity,
                "approved_shares": approved_quantity,
                "funding": funding,
            },
            "put_sales": put_sales,
            "actual_put_sale_proceeds": 0.0,
            "remaining_rebalance_credit": 0.0,
        }
        harvest_plans.append(plan)
        plan_index = len(harvest_plans) - 1
        self._record_tail_state(
            "harvest_requested",
            tranches=tranches,
            entry_history=entry_history,
            harvest_plans=harvest_plans,
            action_plan_id=plan_id,
            action_symbol=symbol,
        )
        try:
            await self._enqueue_harvest_sales(
                plan_index=plan_index,
                tranches=tranches,
                entry_history=entry_history,
                harvest_plans=harvest_plans,
                portfolio_positions=portfolio_positions,
            )
        except Exception as exc:
            log.warning(
                f"{symbol}: Unable to enqueue tail harvest "
                f"({type(exc).__name__}); will retry on a later run."
            )
        if harvest_plans[plan_index].get("status") == "canceled":
            return False

        log.notice(
            f"{symbol}: Tail-put harvest requested for up to "
            f"{dfmt(approved_buy_amount)} of an ordinary-liquidity shortfall; "
            "stock buy deferred to a later run."
        )
        summary["shares_to_trade"] = 0
        summary["action"] = "[magenta]Harvest puts; defer buy"
        return True

    async def _apply_tail_harvest_lifecycle(
        self,
        *,
        orders: List[Tuple[str, str, int]],
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
        market_prices: Dict[str, float],
        regime_summary: List[Dict[str, Any]],
        rebalance_mode: str,
        hard_underweight_symbols: set[str],
        tail_state: tuple[
            list[dict[str, Any]],
            list[dict[str, Any]],
            list[dict[str, Any]],
        ],
    ) -> tuple[List[Tuple[str, str, int]], float]:
        self._active_harvest_symbols.clear()
        self._pending_harvest_stock_buys.clear()
        if self.data_store is None:
            return orders, 0.0

        tranches, entry_history, harvest_plans = tail_state
        if not tranches and not harvest_plans:
            return orders, 0.0

        credit_ready_changed = False
        for plan_index, raw_plan in enumerate(harvest_plans):
            if raw_plan.get("status") != "proceeds_realized":
                continue
            plan = dict(raw_plan)
            plan["status"] = "rebalance_credit_ready"
            plan["rebalance_credit_ready_at"] = self._now()
            harvest_plans[plan_index] = plan
            credit_ready_changed = True
        if credit_ready_changed:
            self._record_tail_state(
                "rebalance_credit_ready",
                tranches=tranches,
                entry_history=entry_history,
                harvest_plans=harvest_plans,
            )

        tail_targets = (
            {target.symbol for target in self.config.strategies.tail_hedge.targets}
            if self._tail_harvesting_enabled()
            else set()
        )
        removed_target_plan_changed = False
        for plan_index, raw_plan in enumerate(harvest_plans):
            if raw_plan.get("symbol") in tail_targets:
                continue
            status = raw_plan.get("status")
            if status not in {"harvest_requested", "rebalance_credit_ready"}:
                continue
            plan = dict(raw_plan)
            if status == "harvest_requested":
                plan["status"] = "canceled"
                plan["canceled_at"] = self._now()
                plan["cancel_reason"] = "tail_target_removed"
            else:
                plan["status"] = "completed"
                plan["completed_at"] = self._now()
                plan["completion_reason"] = "tail_target_removed"
                plan["unused_proceeds"] = float(
                    plan.get("remaining_rebalance_credit", 0.0) or 0.0
                )
            harvest_plans[plan_index] = plan
            removed_target_plan_changed = True
        if removed_target_plan_changed:
            self._record_tail_state(
                "harvest_target_removed",
                tranches=tranches,
                entry_history=entry_history,
                harvest_plans=harvest_plans,
            )
        for plan_index, plan in enumerate(harvest_plans):
            if plan.get("status") != "harvest_requested":
                continue
            try:
                await self._enqueue_harvest_sales(
                    plan_index=plan_index,
                    tranches=tranches,
                    entry_history=entry_history,
                    harvest_plans=harvest_plans,
                    portfolio_positions=portfolio_positions,
                )
            except Exception as exc:
                log.warning(
                    f"{plan.get('symbol', 'unknown')}: Unable to enqueue pending "
                    f"tail harvest ({type(exc).__name__}); will retry on a later run."
                )
        summaries = {str(item["symbol"]): item for item in regime_summary}
        active_by_symbol = {
            str(plan["symbol"]): (index, plan)
            for index, plan in enumerate(harvest_plans)
            if plan.get("status") in TAIL_HEDGE_ACTIVE_HARVEST_STATUSES
        }
        retained_orders: list[tuple[str, str, int]] = []
        symbols_with_actionable_buys: set[str] = set()
        harvest_candidates: list[dict[str, Any]] = []

        for symbol, primary_exchange, quantity in orders:
            active = active_by_symbol.get(symbol)
            if active is not None:
                plan_index, raw_plan = active
                plan = dict(raw_plan)
                status = plan.get("status")
                if status in {
                    "harvest_requested",
                    "put_sell_working",
                    "stock_buy_enqueued",
                }:
                    log.notice(
                        f"{symbol}: Tail-harvest plan is {status}; suppressing "
                        "simultaneous stock trading."
                    )
                    if symbol in summaries:
                        summaries[symbol]["shares_to_trade"] = 0
                        summaries[symbol]["action"] = f"[magenta]Tail harvest: {status}"
                    continue
                if status == "rebalance_credit_ready":
                    if quantity <= 0:
                        plan["status"] = "completed"
                        plan["completed_at"] = self._now()
                        plan["completion_reason"] = "current_buy_not_actionable"
                        plan["unused_proceeds"] = float(
                            plan.get("remaining_rebalance_credit", 0.0) or 0.0
                        )
                        harvest_plans[plan_index] = plan
                        self._record_tail_state(
                            "harvest_completed",
                            tranches=tranches,
                            entry_history=entry_history,
                            harvest_plans=harvest_plans,
                            action_plan_id=plan["plan_id"],
                        )
                        retained_orders.append((symbol, primary_exchange, quantity))
                        continue
                    current_buy_amount = quantity * market_prices[symbol]
                    maximum_buy_amount = min(
                        float(plan.get("remaining_rebalance_credit", 0.0) or 0.0),
                        float(plan.get("approved_buy_amount", 0.0) or 0.0),
                        current_buy_amount,
                    )
                    funded_quantity = min(
                        quantity,
                        math.floor(maximum_buy_amount / market_prices[symbol]),
                    )
                    minimum_shares = int(
                        summaries.get(symbol, {}).get("minimum_trade_shares", 1) or 1
                    )
                    minimum_amount = summaries.get(symbol, {}).get(
                        "minimum_trade_amount"
                    )
                    funded_amount = funded_quantity * market_prices[symbol]
                    if funded_quantity < minimum_shares or (
                        isinstance(minimum_amount, (int, float))
                        and funded_amount < float(minimum_amount)
                    ):
                        plan["status"] = "completed"
                        plan["completed_at"] = self._now()
                        plan["completion_reason"] = (
                            "funded_buy_below_current_minimum_threshold"
                        )
                        plan["unused_proceeds"] = float(
                            plan.get("remaining_rebalance_credit", 0.0) or 0.0
                        )
                        harvest_plans[plan_index] = plan
                        self._record_tail_state(
                            "harvest_completed",
                            tranches=tranches,
                            entry_history=entry_history,
                            harvest_plans=harvest_plans,
                            action_plan_id=plan["plan_id"],
                        )
                        continue
                    plan["revalidated_at"] = self._now()
                    plan["current_approved_buy_amount"] = round(current_buy_amount, 2)
                    plan["maximum_rebalance_buy_amount"] = round(maximum_buy_amount, 2)
                    harvest_plans[plan_index] = plan
                    self._pending_harvest_stock_buys[symbol] = {
                        "plan_index": plan_index,
                        "maximum_buy_amount": maximum_buy_amount,
                        "minimum_trade_shares": minimum_shares,
                        "minimum_trade_amount": minimum_amount,
                        "tranches": tranches,
                        "entry_history": entry_history,
                        "harvest_plans": harvest_plans,
                    }
                    retained_orders.append((symbol, primary_exchange, funded_quantity))
                    if symbol in summaries:
                        summaries[symbol]["shares_to_trade"] = funded_quantity
                        summaries[symbol]["action"] = (
                            f"[green]Buy {funded_quantity} from tail credit"
                        )
                    symbols_with_actionable_buys.add(symbol)
                    continue

            retained_orders.append((symbol, primary_exchange, quantity))
            if (
                quantity <= 0
                or symbol not in tail_targets
                or symbol not in hard_underweight_symbols
            ):
                continue
            put_sales = self._select_profitable_put_sales(
                symbol=symbol,
                tranches=tranches,
                harvest_plans=harvest_plans,
                portfolio_positions=portfolio_positions,
            )
            if not put_sales:
                continue
            harvest_candidates.append(
                {
                    "symbol": symbol,
                    "primary_exchange": primary_exchange,
                    "quantity": quantity,
                    "buy_amount": quantity * market_prices[symbol],
                    "put_sales": put_sales,
                }
            )

        for plan_index, raw_plan in enumerate(harvest_plans):
            if raw_plan.get("status") != "rebalance_credit_ready":
                continue
            symbol = str(raw_plan["symbol"])
            if symbol in symbols_with_actionable_buys:
                continue
            plan = dict(raw_plan)
            plan["status"] = "completed"
            plan["completed_at"] = self._now()
            plan["completion_reason"] = "current_buy_not_actionable"
            plan["unused_proceeds"] = float(
                plan.get("remaining_rebalance_credit", 0.0) or 0.0
            )
            harvest_plans[plan_index] = plan
            self._record_tail_state(
                "harvest_completed",
                tranches=tranches,
                entry_history=entry_history,
                harvest_plans=harvest_plans,
                action_plan_id=plan["plan_id"],
            )

        funding = self._ordinary_rebalance_funding(
            orders=retained_orders,
            account_summary=account_summary,
            portfolio_positions=portfolio_positions,
            market_prices=market_prices,
            harvest_plans=harvest_plans,
        )
        funding_shortfall = funding["funding_shortfall"]
        if funding_shortfall > 0 and harvest_candidates:
            candidate_buy_total = sum(
                float(candidate["buy_amount"]) for candidate in harvest_candidates
            )
            allocatable_shortfall = min(funding_shortfall, candidate_buy_total)
            allocated_amount = 0.0
            for index, candidate in enumerate(harvest_candidates):
                symbol = str(candidate["symbol"])
                quantity = int(candidate["quantity"])
                market_price = market_prices[symbol]
                if index == len(harvest_candidates) - 1:
                    allocation = allocatable_shortfall - allocated_amount
                else:
                    allocation = allocatable_shortfall * (
                        float(candidate["buy_amount"]) / candidate_buy_total
                    )
                    allocated_amount += allocation
                approved_quantity = min(quantity, math.floor(allocation / market_price))
                summary = summaries.get(symbol, {})
                minimum_shares = int(summary.get("minimum_trade_shares", 1) or 1)
                minimum_amount = summary.get("minimum_trade_amount")
                approved_amount = approved_quantity * market_price
                if approved_quantity < minimum_shares or (
                    isinstance(minimum_amount, (int, float))
                    and approved_amount < float(minimum_amount)
                ):
                    continue

                requested = await self._request_tail_harvest(
                    symbol=symbol,
                    approved_quantity=approved_quantity,
                    ordinary_approved_quantity=quantity,
                    market_price=market_price,
                    put_sales=candidate["put_sales"],
                    funding={
                        **funding,
                        "candidate_buy_total": round(candidate_buy_total, 2),
                        "allocated_shortfall": round(allocation, 2),
                    },
                    tranches=tranches,
                    entry_history=entry_history,
                    harvest_plans=harvest_plans,
                    portfolio_positions=portfolio_positions,
                    summary=summary,
                    rebalance_mode=rebalance_mode,
                )
                if requested:
                    retained_orders.remove(
                        (
                            symbol,
                            str(candidate["primary_exchange"]),
                            quantity,
                        )
                    )

        self._active_harvest_symbols.update(active_tail_harvest_symbols(harvest_plans))
        return retained_orders, self._harvest_reserved_cash(harvest_plans)

    def prepare_regime_order(
        self,
        symbol: str,
        quantity: int,
        limit_price: float,
    ) -> tuple[int, str]:
        pending = self._pending_harvest_stock_buys.get(symbol)
        if pending is None:
            return quantity, f"{self.regime_rebalance_order_ref_prefix}:{symbol}"

        maximum_buy_amount = float(pending["maximum_buy_amount"])
        funded_quantity = min(quantity, math.floor(maximum_buy_amount / limit_price))
        minimum_shares = int(pending.get("minimum_trade_shares", 1) or 1)
        minimum_amount = pending.get("minimum_trade_amount")
        funded_amount = funded_quantity * limit_price
        harvest_plans = pending["harvest_plans"]
        plan_index = int(pending["plan_index"])
        plan = dict(harvest_plans[plan_index])
        if funded_quantity < minimum_shares or (
            isinstance(minimum_amount, (int, float))
            and funded_amount < float(minimum_amount)
        ):
            plan["status"] = "completed"
            plan["completed_at"] = self._now()
            plan["completion_reason"] = "live_order_below_minimum_threshold"
            plan["unused_proceeds"] = float(
                plan.get("remaining_rebalance_credit", 0.0) or 0.0
            )
            harvest_plans[plan_index] = plan
            self._record_tail_state(
                "harvest_completed",
                tranches=pending["tranches"],
                entry_history=pending["entry_history"],
                harvest_plans=harvest_plans,
                action_plan_id=plan["plan_id"],
            )
            self._reserve_cash_for_post_management(
                self._flow_reserved_cash + self._harvest_reserved_cash(harvest_plans)
            )
            return 0, ""

        order_ref = f"{self.regime_rebalance_order_ref_prefix}:{plan['plan_id']}"
        authorized_amount = round(funded_quantity * limit_price, 2)
        plan["status"] = "stock_buy_enqueued"
        plan["stock_buy"] = {
            "order_ref": order_ref,
            "order_status": "enqueued",
            "quantity": funded_quantity,
            "limit_price": limit_price,
            "authorized_amount": authorized_amount,
            "enqueued_at": self._now(),
            "filled_quantity": 0.0,
            "actual_cost": 0.0,
        }
        harvest_plans[plan_index] = plan
        self._record_tail_state(
            "stock_buy_enqueued",
            tranches=pending["tranches"],
            entry_history=pending["entry_history"],
            harvest_plans=harvest_plans,
            action_plan_id=plan["plan_id"],
            order_ref=order_ref,
        )
        return funded_quantity, order_ref

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

    def get_buying_power(self, account_summary: Dict[str, AccountValue]) -> int:
        return self._get_buying_power(account_summary)

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
        exec_filter = ExecutionFilter(time=start_time.strftime("%Y%m%d %H:%M:%S"))

        if self.data_store:
            fills = await self.ibkr.request_executions(exec_filter)
            self._recent_execution_fills = list(fills)
            self.data_store.record_executions(fills)
            return self.data_store.get_last_regime_rebalance_time(
                symbols,
                self.regime_rebalance_order_ref_prefix,
                start_time,
            )

        fills = await self.ibkr.request_executions(exec_filter)
        self._recent_execution_fills = list(fills)
        last_rebalance: Optional[datetime] = None
        for fill in fills:
            execution = fill.execution
            if not execution.orderRef:
                continue
            if not execution.orderRef.startswith(
                self.regime_rebalance_order_ref_prefix
            ):
                continue
            if fill.contract.symbol not in symbols:
                continue
            fill_time = fill.time or execution.time
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
        self._reserve_cash_for_post_management(0.0)
        self._flow_reserved_cash = 0.0
        self._recent_execution_fills = []
        self._pending_harvest_stock_buys.clear()
        self._approved_buy_symbols.clear()
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
        tail_state: tuple[
            list[dict[str, Any]],
            list[dict[str, Any]],
            list[dict[str, Any]],
        ] = ([], [], [])
        if self.data_store is not None:
            tail_state = self._load_reconciled_tail_state()
        active_harvest_credit = self._active_harvest_credit(tail_state[2])

        weight_base = regime_rebalance.weight_base
        regime_margin_usage = self._resolve_regime_margin_usage()
        if weight_base == RegimeRebalanceBaseEnum.managed_stocks:
            total_value = sum(current_values.values())
        elif weight_base == RegimeRebalanceBaseEnum.net_liq_ex_options:
            excluded_value = 0.0
            for positions in portfolio_positions.values():
                for position in positions:
                    if isinstance(position.contract, Option):
                        market_value = float(position.marketValue or 0.0)
                        excluded_value += market_value
            net_liq = float(account_summary["NetLiquidation"].value)
            adjusted_net_liq = net_liq - excluded_value - active_harvest_credit
            total_value = math.floor(adjusted_net_liq * regime_margin_usage)
            log.notice(
                "Regime rebalancing base: mode=net_liq_ex_options "
                f"net_liq={dfmt(net_liq)} excluded_options={dfmt(excluded_value)} "
                f"excluded_tail_credit={dfmt(active_harvest_credit)} "
                f"margin_usage={ffmt(regime_margin_usage)} "
                f"base={dfmt(total_value)}"
            )
        else:
            net_liq = float(account_summary["NetLiquidation"].value)
            excluded_tail_hedge_value = self._tail_hedge_market_value(
                portfolio_positions
            )
            adjusted_net_liq = (
                net_liq - excluded_tail_hedge_value - active_harvest_credit
            )
            total_value = math.floor(adjusted_net_liq * regime_margin_usage)
            log.notice(
                "Regime rebalancing base: mode=net_liq "
                f"net_liq={dfmt(net_liq)} "
                f"excluded_tail_hedges={dfmt(excluded_tail_hedge_value)} "
                f"excluded_tail_credit={dfmt(active_harvest_credit)} "
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

        to_trade, harvest_reserved_cash = await self._apply_tail_harvest_lifecycle(
            orders=to_trade,
            account_summary=account_summary,
            portfolio_positions=portfolio_positions,
            market_prices=market_prices,
            regime_summary=regime_summary,
            rebalance_mode=rebalance_mode,
            hard_underweight_symbols=hard_underweight_symbols,
            tail_state=tail_state,
        )
        self._approved_buy_symbols.update(
            symbol for symbol, _primary_exchange, quantity in to_trade if quantity > 0
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
                    and symbol not in self._active_harvest_symbols
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
        self._flow_reserved_cash = reserved_cash_for_post_management
        reserved_cash_for_post_management += harvest_reserved_cash
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
