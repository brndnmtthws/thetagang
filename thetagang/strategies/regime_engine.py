from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import exchange_calendars as xcals
import numpy as np
import pandas as pd
from ib_async import ExecutionFilter, PortfolioItem, Ticker
from ib_async.contract import Option, Stock
from rich.table import Table

from thetagang import log
from thetagang.accounting import (
    AccountingPolicy,
    AccountSummary,
    BrokerAccountSnapshot,
    CapitalBaseKind,
    PortfolioAccounting,
    RegimeRebalanceBaseEnum,
    owned_option_market_value,
    state_owned_option_values,
)
from thetagang.config import Config
from thetagang.config_models import (
    DecisionMarketDataConfig,
    TailHarvestDecisionConfig,
    TargetWeightPolicyConfig,
)
from thetagang.db import DataStore
from thetagang.external_decisions import (
    ExternalDecisionError,
    ExternalDecisionMarketData,
    ExternalDecisionProviders,
    ExternalDecisionResponse,
    external_decision_response_metadata,
    validate_decision_expiry,
)
from thetagang.fmt import dfmt, ffmt, ifmt, pfmt
from thetagang.ibkr import IBKR, TickerField
from thetagang.strategies.runtime_services import resolve_symbol_configs
from thetagang.strategies.tail_harvest_policy import (
    HarvestBandDecision,
    evaluate_harvest_band,
    minimum_harvest_limit_price,
)
from thetagang.strategies.tail_hedge_state import (
    TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX,
    TAIL_HEDGE_MIN_LIMIT_PRICE_ATTR,
    TailHedgeCohort,
    TailHedgeState,
    TailHedgeStateStore,
    build_tail_reduction_order_ref,
    parse_state_datetime,
)
from thetagang.tail_harvest_decision import (
    HarvestCandidateInput,
    TailProgramInput,
    build_tail_harvest_request,
    validate_tail_harvest_response,
)
from thetagang.target_weight_policy import (
    TARGET_WEIGHT_POLICY_STATE_EVENT,
    TargetWeightMultiplier,
    apply_target_weight_adjustments,
    build_target_weight_request,
    validate_target_weight_response,
)
from thetagang.trading_operations import OrderOperations
from thetagang.util import midpoint_or_market_price, portfolio_positions_to_dict

AlignedClosesResult = tuple[list[date], dict[str, list[float]]]


class AlignedClosesFetcher(Protocol):
    def __call__(
        self,
        symbols: list[str],
        lookback_days: int,
        cooldown_days: int,
        *,
        primary_exchanges: dict[str, str] | None = None,
    ) -> Coroutine[Any, Any, AlignedClosesResult]: ...


ClosesBySymbol = dict[str, dict[date, float]]
TRADING_DAYS_PER_YEAR = 252
REGIME_HISTORY_TIMEFRAME = "1 day"
REGIME_HISTORY_MAX_ATTEMPTS = 3
REGIME_HISTORY_RETRY_DELAY_SECONDS = 0.25
ABSOLUTE_TREND_STATE_EVENT = "absolute_trend_state"
TAIL_HEDGE_HARVEST_EVENT = "tail_hedge_harvest"
TAIL_HEDGE_HARVEST_SCHEMA_VERSION = 2


class RegimeHistoryValidationError(ValueError):
    def __init__(self, message: str, *, cache_recoverable: bool) -> None:
        super().__init__(message)
        self.cache_recoverable = cache_recoverable


@dataclass(frozen=True)
class _TargetWeightPolicyOutcome:
    adjustments: dict[str, TargetWeightMultiplier] | None
    response: ExternalDecisionResponse | None
    error: str | None


@dataclass(frozen=True)
class _TailHarvestSnapshot:
    portfolio_positions: dict[str, list[PortfolioItem]]
    state: TailHedgeState
    cohorts: list[TailHedgeCohort]
    candidates: list[HarvestPut]
    live_quotes: dict[tuple[str, int], float]
    net_liquidation: float
    regime_base: float
    excluded_option_value: float
    band: HarvestBandDecision


@dataclass(frozen=True)
class RatioGateResult:
    ok: bool
    reason: str
    anchor: str
    rest: list[str]
    weights: dict[str, float]
    daily_mean: float | None
    daily_std: float | None
    daily_var: float | None
    annualized_vol: float | None
    vol_min: float
    tstat: float
    drift_max: float

    def to_payload(self, *, enabled: bool) -> dict[str, Any]:
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
class _AbsoluteTrendSignal:
    lookback_days: int
    latest_session: str
    latest_close: float
    moving_average: float
    momentum_reference_close: float
    lookback_return: float
    risk_off: bool

    @classmethod
    def from_history(
        cls,
        *,
        dates: list[date],
        closes: list[float],
        lookback_days: int,
    ) -> _AbsoluteTrendSignal:
        required_points = lookback_days + 1
        if len(dates) != len(closes):
            raise ValueError("misaligned_history")
        if len(dates) < required_points or len(closes) < required_points:
            raise ValueError("insufficient_history")

        window = np.asarray(closes[-required_points:], dtype=float)
        if not np.all(np.isfinite(window)) or np.any(window <= 0):
            raise ValueError("invalid_closes")

        latest_close = float(window[-1])
        moving_average = float(np.mean(window[:-1]))
        momentum_reference_close = float(window[0])
        return cls(
            lookback_days=lookback_days,
            latest_session=str(dates[-1]),
            latest_close=latest_close,
            moving_average=moving_average,
            momentum_reference_close=momentum_reference_close,
            lookback_return=latest_close / momentum_reference_close - 1.0,
            risk_off=(
                latest_close < moving_average
                and latest_close < momentum_reference_close
            ),
        )

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        lookback_days: int,
    ) -> _AbsoluteTrendSignal:
        if not isinstance(payload, dict):
            raise TypeError("state_not_an_object")
        if payload.get("lookback_days") != lookback_days:
            raise ValueError("lookback_mismatch")

        latest_session = payload.get("latest_session")
        risk_off = payload.get("risk_off")
        if not isinstance(latest_session, str):
            raise TypeError("invalid_latest_session")
        if not latest_session:
            raise ValueError("invalid_latest_session")
        try:
            date.fromisoformat(latest_session)
        except ValueError as exc:
            raise ValueError("invalid_latest_session") from exc
        if not isinstance(risk_off, bool):
            raise TypeError("invalid_risk_state")

        def finite_number(field: str, *, positive: bool = False) -> float:
            value = payload.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"invalid_{field}")
            normalized_value = float(value)
            if not math.isfinite(normalized_value) or (
                positive and normalized_value <= 0
            ):
                raise ValueError(f"invalid_{field}")
            return normalized_value

        latest_close = finite_number("latest_close", positive=True)
        moving_average = finite_number("moving_average", positive=True)
        momentum_reference_close = finite_number(
            "momentum_reference_close", positive=True
        )
        lookback_return = finite_number("lookback_return")
        if not math.isclose(
            lookback_return,
            latest_close / momentum_reference_close - 1.0,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("inconsistent_lookback_return")
        if risk_off != (
            latest_close < moving_average and latest_close < momentum_reference_close
        ):
            raise ValueError("inconsistent_risk_state")

        return cls(
            lookback_days=lookback_days,
            latest_session=latest_session,
            latest_close=latest_close,
            moving_average=moving_average,
            momentum_reference_close=momentum_reference_close,
            lookback_return=lookback_return,
            risk_off=risk_off,
        )

    @property
    def state(self) -> str:
        return "risk_off" if self.risk_off else "risk_on"

    def target_details(
        self,
        *,
        pre_trend_target: float,
        risk_off_multiplier: float,
        history_source: str,
        history_failure: str | None = None,
    ) -> dict[str, Any]:
        applied_multiplier = risk_off_multiplier if self.risk_off else 1.0
        details: dict[str, Any] = {
            "lookback_days": self.lookback_days,
            "latest_session": self.latest_session,
            "latest_close": self.latest_close,
            "moving_average": self.moving_average,
            "momentum_reference_close": self.momentum_reference_close,
            "lookback_return": self.lookback_return,
            "risk_off": self.risk_off,
            "state": self.state,
            "pre_trend_target": pre_trend_target,
            "final_target": pre_trend_target * applied_multiplier,
            "applied_multiplier": applied_multiplier,
            "history_source": history_source,
        }
        if history_failure is not None:
            details["history_failure"] = history_failure
        return details


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

    @property
    def profit_multiple(self) -> float:
        return self.net_proceeds_per_contract / self.cost_basis_per_contract


@dataclass(frozen=True)
class PlannedHarvest:
    candidate: HarvestPut
    quantity: int
    order: Any
    minimum_limit_price: float
    minimum_net_proceeds: float


def _ffmt_or_dash(value: float | None, precision: int = 2) -> str:
    return ffmt(value, precision) if value is not None else "-"


def _pfmt_or_dash(value: float | None) -> str:
    return pfmt(value) if value is not None else "-"


class RegimeHistoryCache:
    def __init__(self, fetcher: AlignedClosesFetcher) -> None:
        self._fetcher = fetcher
        self._cache: dict[
            tuple[tuple[str, ...], int, int, tuple[tuple[str, str], ...]],
            AlignedClosesResult,
        ] = {}

    async def get(
        self,
        symbols: list[str],
        lookback_days: int,
        cooldown_days: int,
        *,
        primary_exchanges: dict[str, str] | None = None,
    ) -> AlignedClosesResult:
        exchange_key = tuple(sorted((primary_exchanges or {}).items()))
        key = (tuple(symbols), lookback_days, cooldown_days, exchange_key)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if primary_exchanges:
            result = await self._fetcher(
                symbols,
                lookback_days,
                cooldown_days,
                primary_exchanges=primary_exchanges,
            )
        else:
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
        data_store: DataStore | None,
        external_decisions: ExternalDecisionProviders | None = None,
        dry_run: bool = False,
        get_primary_exchange: Callable[[str], str],
        now_provider: Callable[[], datetime],
        tail_hedge_stage_enabled: Callable[[], bool] | None = None,
        set_reserved_cash_for_post_management: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.order_ops = order_ops
        self.data_store = data_store
        self.external_decisions = external_decisions or ExternalDecisionProviders()
        self.dry_run = dry_run
        self._target_weight_policy_outcome: _TargetWeightPolicyOutcome | None = None
        self._get_primary_exchange = get_primary_exchange
        self._now = now_provider
        self._tail_hedge_stage_enabled = tail_hedge_stage_enabled or (lambda: False)
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

    def begin_run(self) -> None:
        """Clear decisions that may only be reused within one manager run."""

        self._target_weight_policy_outcome = None

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

    def _latest_net_liquidation(self, fallback: float) -> float:
        try:
            return self.ibkr.cached_net_liquidation(self.config.runtime.account.number)
        except RuntimeError:
            # Never substitute a cash-derived proxy for an unavailable NLV.
            return fallback

    def _regime_rebalance_base_value(
        self,
        *,
        net_liquidation: float,
        portfolio_positions: dict[str, list[PortfolioItem]],
        market_prices: dict[str, float],
        cohorts: list[TailHedgeCohort],
        tail_hedge_value_override: float | None = None,
    ) -> tuple[float, float]:
        """Return the configured regime base and its signed option exclusion."""
        accounting = PortfolioAccounting.from_net_liquidation(
            config=self.config,
            net_liquidation=net_liquidation,
            portfolio_positions=portfolio_positions,
            tail_owned_quantities={
                cohort.con_id: cohort.quantity for cohort in cohorts
            },
            regime_symbols=market_prices,
        )
        base = accounting.capital_base(
            CapitalBaseKind.REGIME_REBALANCE,
            market_prices=market_prices,
            tail_hedge_value_override=tail_hedge_value_override,
        )
        return base.value, base.excluded_value

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

    @staticmethod
    def _owned_tail_position_value(
        position: PortfolioItem,
        cohort: TailHedgeCohort,
    ) -> tuple[int, float] | None:
        if position.contract.conId != cohort.con_id:
            return None
        return owned_option_market_value(position, cohort.quantity)

    @classmethod
    def _tail_hedge_market_value(
        cls,
        portfolio_positions: dict[str, list[PortfolioItem]],
        cohorts: list[TailHedgeCohort],
    ) -> float:
        return sum(
            state_owned_option_values(
                portfolio_positions,
                {cohort.con_id: cohort.quantity for cohort in cohorts},
            ).values()
        )

    @staticmethod
    def _long_puts_by_con_id(
        portfolio_positions: dict[str, list[PortfolioItem]],
    ) -> dict[int, PortfolioItem]:
        return {
            int(position.contract.conId): position
            for positions in portfolio_positions.values()
            for position in positions
            if isinstance(position.contract, Option)
            and position.contract.right.upper().startswith("P")
            and type(position.contract.conId) is int
            and position.contract.conId > 0
            and float(position.position) > 0
        }

    @classmethod
    def _tail_hedge_market_value_at_quotes(
        cls,
        portfolio_positions: dict[str, list[PortfolioItem]],
        cohorts: list[TailHedgeCohort],
        quoted_limit_prices: dict[tuple[str, int], float],
    ) -> float:
        sleeve_value = cls._tail_hedge_market_value(portfolio_positions, cohorts)
        cohorts_by_key = {
            (cohort.entry_id, cohort.con_id): cohort for cohort in cohorts
        }
        positions_by_con_id = cls._long_puts_by_con_id(portfolio_positions)
        for key, limit_price in quoted_limit_prices.items():
            cohort = cohorts_by_key.get(key)
            position = positions_by_con_id.get(key[1])
            if cohort is None or position is None:
                continue
            owned_position = cls._owned_tail_position_value(position, cohort)
            if owned_position is None:
                continue
            owned_quantity, reported_owned_value = owned_position
            try:
                multiplier = float(position.contract.multiplier)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(multiplier) or multiplier <= 0:
                continue
            quoted_owned_value = limit_price * multiplier * owned_quantity
            sleeve_value -= max(0.0, reported_owned_value - quoted_owned_value)
        return sleeve_value

    def _option_order_state(
        self,
        owned_con_ids: set[int] | None = None,
    ) -> tuple[set[int], set[str]]:
        account_number = self.config.runtime.account.number
        unavailable_con_ids: set[int] = set()
        tail_sell_symbols: set[str] = set()
        for trade in self.ibkr.open_trades():
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
            unavailable_con_ids.add(contract.conId)
            if (
                owned_con_ids is not None
                and contract.conId in owned_con_ids
                and str(getattr(order, "action", "")).upper() == "SELL"
            ):
                tail_sell_symbols.add(str(contract.symbol))
        for contract, order, _intent_id in self.order_ops.orders.records():
            if (
                not isinstance(contract, Option)
                or type(contract.conId) is not int
                or contract.conId <= 0
            ):
                continue
            unavailable_con_ids.add(contract.conId)
            if (
                owned_con_ids is not None
                and contract.conId in owned_con_ids
                and getattr(order, "account", None) == account_number
                and str(getattr(order, "action", "")).upper() == "SELL"
            ):
                tail_sell_symbols.add(str(contract.symbol))
        return unavailable_con_ids, tail_sell_symbols

    def _tail_harvest_conflicts(
        self,
        cohorts: list[TailHedgeCohort],
    ) -> tuple[set[int], set[str], str | None]:
        owned_con_ids = {cohort.con_id for cohort in cohorts}
        unavailable_con_ids, working_sale_symbols = self._option_order_state(
            owned_con_ids
        )
        pending_recovery_symbols = {
            cohort.symbol for cohort in cohorts if cohort.has_pending_recovery
        }
        blocked_symbols = working_sale_symbols | pending_recovery_symbols
        if pending_recovery_symbols and working_sale_symbols:
            reason = "unresolved_tail_reduction"
        elif pending_recovery_symbols:
            reason = "pending_recovery"
        elif working_sale_symbols:
            reason = "working_tail_sale"
        else:
            reason = None
        return unavailable_con_ids, blocked_symbols, reason

    def _record_tail_harvest_blocked(
        self,
        *,
        rebalance_shares: dict[str, int],
        blocked_symbols: set[str],
        reason: str,
    ) -> None:
        for symbol, quantity in rebalance_shares.items():
            self._record_tail_harvest(
                "harvest_blocked",
                symbol=symbol,
                reason=reason,
                rebalance_shares=quantity,
                blocking_symbols=sorted(blocked_symbols),
            )
        log.info(
            "Skipping a new portfolio tail harvest while prior reductions "
            f"are unresolved for {', '.join(sorted(blocked_symbols))}."
        )

    def _build_profitable_tail_put(
        self,
        *,
        cohort: TailHedgeCohort,
        position: PortfolioItem,
        limit_price: float,
        record_unprofitable: bool = True,
    ) -> HarvestPut | None:
        symbol = cohort.symbol
        contract = position.contract
        if not isinstance(contract, Option) or contract.symbol != symbol:
            return None
        try:
            multiplier = float(contract.multiplier)
            live_quantity = float(position.position)
        except (TypeError, ValueError):
            return None
        if (
            not math.isfinite(multiplier)
            or multiplier <= 0
            or not math.isfinite(live_quantity)
        ):
            return None
        quantity = min(cohort.quantity, math.floor(live_quantity))
        if quantity <= 0:
            return None

        try:
            average_cost = float(getattr(position, "averageCost", 0.0) or 0.0)
        except (TypeError, ValueError):
            average_cost = 0.0
        estimated_fee = self._estimated_tail_fee_per_contract()
        configured_entry_basis = cohort.entry_limit_price * multiplier + estimated_fee
        if not math.isfinite(average_cost) or average_cost <= 0:
            average_cost = configured_entry_basis
        else:
            average_cost = max(average_cost, configured_entry_basis)
        gross_proceeds = limit_price * multiplier
        net_proceeds = max(0.0, gross_proceeds - estimated_fee)
        if (
            not math.isfinite(average_cost)
            or average_cost <= 0
            or net_proceeds <= average_cost
        ):
            if record_unprofitable:
                self._record_tail_harvest(
                    "candidate_not_net_profitable",
                    symbol=symbol,
                    entry_id=cohort.entry_id,
                    con_id=cohort.con_id,
                    gross_proceeds_per_contract=gross_proceeds,
                    estimated_fee_per_contract=estimated_fee,
                    net_proceeds_per_contract=net_proceeds,
                    cost_basis_per_contract=average_cost,
                )
                log.info(
                    f"{symbol}: Tail put conId={cohort.con_id} is not profitable "
                    "after its estimated sell fee."
                )
            return None

        contract.exchange = self.order_ops.get_order_exchange()
        return HarvestPut(
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

    async def _quote_tail_puts(
        self,
        *,
        symbols: set[str],
        cohorts: list[TailHedgeCohort],
        portfolio_positions: dict[str, list[PortfolioItem]],
    ) -> dict[tuple[str, int], float]:
        snapshot_positions = self._long_puts_by_con_id(portfolio_positions)

        quoted: dict[tuple[str, int], float] = {}
        for cohort in cohorts:
            con_id = cohort.con_id
            position = snapshot_positions.get(con_id)
            if (
                cohort.symbol not in symbols
                or cohort.status != "active"
                or cohort.has_pending_recovery
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
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    f"{cohort.symbol}: Unable to quote tail put {con_id} for harvesting "
                    f"({type(exc).__name__})."
                )
                continue
            limit_price = round(float(midpoint_or_market_price(ticker)), 2)
            if math.isfinite(limit_price) and limit_price > 0:
                quoted[(cohort.entry_id, con_id)] = limit_price
        return quoted

    def _build_tail_harvest_snapshot(
        self,
        *,
        net_liquidation: float,
        rebalance_shares: dict[str, int],
        market_prices: dict[str, float],
        quoted_limit_prices: dict[tuple[str, int], float],
        record_unprofitable: bool,
    ) -> _TailHarvestSnapshot | None:
        if self._tail_state_store is None:
            return None

        account_number = self.config.runtime.account.number
        portfolio_positions = portfolio_positions_to_dict(
            self.ibkr.portfolio(account=account_number)
        )
        state = self._tail_state_store.load()
        cohorts = state.open_cohorts
        unavailable_con_ids, blocked_symbols, blocked_reason = (
            self._tail_harvest_conflicts(cohorts)
        )
        if blocked_reason is not None:
            self._record_tail_harvest_blocked(
                rebalance_shares=rebalance_shares,
                blocked_symbols=blocked_symbols,
                reason=blocked_reason,
            )
            return None

        positions_by_con_id = self._long_puts_by_con_id(portfolio_positions)
        cohorts_by_key = {
            (cohort.entry_id, cohort.con_id): cohort for cohort in cohorts
        }
        candidates: list[HarvestPut] = []
        live_quotes: dict[tuple[str, int], float] = {}
        for key, limit_price in quoted_limit_prices.items():
            cohort = cohorts_by_key.get(key)
            con_id = key[1]
            position = positions_by_con_id.get(con_id)
            if (
                cohort is None
                or cohort.status != "active"
                or cohort.has_pending_recovery
                or position is None
                or position.contract.symbol != cohort.symbol
            ):
                continue
            live_quotes[key] = limit_price
            if con_id in unavailable_con_ids:
                continue
            candidate = self._build_profitable_tail_put(
                cohort=cohort,
                position=position,
                limit_price=limit_price,
                record_unprofitable=record_unprofitable,
            )
            if candidate is not None:
                candidates.append(candidate)
        candidates = sorted(
            candidates,
            key=lambda candidate: (
                candidate.expiration,
                -candidate.net_proceeds_per_contract,
                candidate.contract.conId,
            ),
        )
        sleeve_value = self._tail_hedge_market_value_at_quotes(
            portfolio_positions,
            cohorts,
            live_quotes,
        )
        current_net_liquidation = self._latest_net_liquidation(net_liquidation)
        regime_base, excluded_option_value = self._regime_rebalance_base_value(
            net_liquidation=current_net_liquidation,
            portfolio_positions=portfolio_positions,
            market_prices=market_prices,
            cohorts=cohorts,
            # Use the conservative quote-aware tail mark on both sides of the
            # ratio. This keeps the shared option-exclusion formula coherent.
            tail_hedge_value_override=sleeve_value,
        )
        tail_hedge = self.config.strategies.tail_hedge
        band = evaluate_harvest_band(
            portfolio_base_value=regime_base,
            sleeve_value=sleeve_value,
            trigger_weight=tail_hedge.harvest_trigger_weight,
            target_weight=tail_hedge.harvest_target_weight,
        )
        if band is None:
            return None
        return _TailHarvestSnapshot(
            portfolio_positions=portfolio_positions,
            state=state,
            cohorts=cohorts,
            candidates=candidates,
            live_quotes=live_quotes,
            net_liquidation=current_net_liquidation,
            regime_base=regime_base,
            excluded_option_value=excluded_option_value,
            band=band,
        )

    def _tail_harvest_band_payload(
        self,
        snapshot: _TailHarvestSnapshot,
        *,
        rebalance_shares: dict[str, int],
        market_prices: dict[str, float],
    ) -> dict[str, Any]:
        tail_hedge = self.config.strategies.tail_hedge
        return {
            "net_liquidation": snapshot.net_liquidation,
            "regime_rebalance_base": snapshot.regime_base,
            "regime_weight_base": (
                self.config.strategies.regime_rebalance.weight_base.value
            ),
            "excluded_option_value": snapshot.excluded_option_value,
            "sleeve_value": snapshot.band.sleeve_value,
            "sleeve_weight": snapshot.band.sleeve_weight,
            "harvest_trigger_weight": tail_hedge.harvest_trigger_weight,
            "harvest_target_weight": tail_hedge.harvest_target_weight,
            "target_sleeve_value": snapshot.band.target_value,
            "approved_rebalance_value": sum(
                shares * market_prices[symbol]
                for symbol, shares in rebalance_shares.items()
            ),
        }

    @staticmethod
    def _select_tail_harvest_candidates(
        candidates: list[HarvestPut],
        sale_budget: float,
    ) -> list[tuple[HarvestPut, int]]:
        selected: list[tuple[HarvestPut, int]] = []
        selected_con_ids: set[int] = set()
        remaining = sale_budget
        for candidate in candidates:
            con_id = candidate.contract.conId
            if remaining <= 0.0 or con_id in selected_con_ids:
                continue
            # The band measures option market value, so fees must not change
            # how many contracts are removed from the sleeve.
            quantity = min(
                candidate.quantity,
                math.ceil(remaining / candidate.gross_proceeds_per_contract),
            )
            if quantity <= 0:
                continue
            selected.append((candidate, quantity))
            selected_con_ids.add(con_id)
            remaining -= quantity * candidate.gross_proceeds_per_contract
        return selected

    def _tail_harvest_planned_sales(
        self,
        snapshot: _TailHarvestSnapshot,
    ) -> list[dict[str, Any]]:
        return [
            {
                "entry_id": candidate.entry_id,
                "symbol": candidate.contract.symbol,
                "con_id": candidate.contract.conId,
                "expiration": candidate.expiration,
                "quantity": quantity,
                "limit_price": candidate.limit_price,
                "estimated_gross_proceeds": (
                    quantity * candidate.gross_proceeds_per_contract
                ),
                "estimated_fees": quantity * candidate.estimated_fee_per_contract,
                "estimated_net_proceeds": (
                    quantity * candidate.net_proceeds_per_contract
                ),
            }
            for candidate, quantity in self._select_tail_harvest_candidates(
                snapshot.candidates,
                snapshot.band.sale_budget,
            )
        ]

    @staticmethod
    def _finite_float_or_none(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    def _tail_harvest_hedge_inputs(
        self, snapshot: _TailHarvestSnapshot
    ) -> list[dict[str, Any]]:
        positions_by_con_id = self._long_puts_by_con_id(snapshot.portfolio_positions)
        candidates_by_key = {
            (candidate.entry_id, candidate.contract.conId): candidate
            for candidate in snapshot.candidates
        }
        inputs: list[dict[str, Any]] = []
        for cohort in snapshot.cohorts:
            key = (cohort.entry_id, cohort.con_id)
            position = positions_by_con_id.get(cohort.con_id)
            owned_position = (
                self._owned_tail_position_value(position, cohort)
                if position is not None
                else None
            )
            owned_quantity = owned_position[0] if owned_position is not None else 0
            reported_owned_value = (
                owned_position[1] if owned_position is not None else None
            )
            live_quantity = self._finite_float_or_none(
                getattr(position, "position", None)
            )
            live_market_value = self._finite_float_or_none(
                getattr(position, "marketValue", None)
            )
            live_unrealized_pnl = self._finite_float_or_none(
                getattr(position, "unrealizedPNL", None)
            )
            live_realized_pnl = self._finite_float_or_none(
                getattr(position, "realizedPNL", None)
            )
            live_average_cost = self._finite_float_or_none(
                getattr(position, "averageCost", None)
            )
            ownership_ratio = (
                owned_quantity / live_quantity
                if live_quantity is not None and live_quantity > 0
                else None
            )
            candidate = candidates_by_key.get(key)
            inputs.append(
                {
                    "entry_id": cohort.entry_id,
                    "symbol": cohort.symbol,
                    "status": cohort.status,
                    "contract": {
                        "con_id": cohort.con_id,
                        "expiration": cohort.expiration,
                        "strike": cohort.strike,
                        "right": "P",
                        "multiplier": self._finite_float_or_none(
                            getattr(
                                getattr(position, "contract", None),
                                "multiplier",
                                None,
                            )
                        ),
                    },
                    "state_owned_quantity": owned_quantity,
                    "live_position_quantity": live_quantity,
                    "reported_owned_market_value": reported_owned_value,
                    "live_position_market_value": live_market_value,
                    "live_average_cost_per_contract": live_average_cost,
                    "live_realized_pnl": live_realized_pnl,
                    "state_owned_unrealized_pnl": (
                        live_unrealized_pnl * ownership_ratio
                        if live_unrealized_pnl is not None
                        and ownership_ratio is not None
                        else None
                    ),
                    "quoted_limit_price": snapshot.live_quotes.get(key),
                    "entered_at": self._as_utc(cohort.entered_at).isoformat(),
                    "entry_limit_price": cohort.entry_limit_price,
                    "estimated_cost": cohort.estimated_cost,
                    "recovered_cost": cohort.recovered_cost,
                    "unrecovered_cost": cohort.net_charge,
                    "host_candidate": candidate is not None,
                    "candidate": (
                        HarvestCandidateInput.model_validate(
                            candidate, from_attributes=True
                        ).model_dump(mode="json")
                        if candidate is not None
                        else None
                    ),
                }
            )
        return inputs

    def _tail_harvest_underlying_inputs(
        self,
        snapshot: _TailHarvestSnapshot,
        *,
        rebalance_shares: dict[str, int],
        market_prices: dict[str, float],
        regime_summary: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        summaries = {str(item["symbol"]): item for item in regime_summary}
        result: dict[str, dict[str, Any]] = {}
        for target in self.config.strategies.tail_hedge.targets:
            symbol = target.symbol
            summary = summaries.get(symbol, {})
            stock_positions = [
                position
                for position in snapshot.portfolio_positions.get(symbol, [])
                if isinstance(position.contract, Stock)
            ]
            live_shares = sum(
                self._finite_float_or_none(position.position) or 0.0
                for position in stock_positions
            )
            reported_market_value = sum(
                self._finite_float_or_none(position.marketValue) or 0.0
                for position in stock_positions
            )
            reported_unrealized_pnl = sum(
                self._finite_float_or_none(getattr(position, "unrealizedPNL", None))
                or 0.0
                for position in stock_positions
            )
            reported_realized_pnl = sum(
                self._finite_float_or_none(getattr(position, "realizedPNL", None))
                or 0.0
                for position in stock_positions
            )
            gross_shares = sum(
                abs(self._finite_float_or_none(position.position) or 0.0)
                for position in stock_positions
            )
            weighted_cost = sum(
                abs(self._finite_float_or_none(position.position) or 0.0)
                * (
                    self._finite_float_or_none(getattr(position, "averageCost", None))
                    or 0.0
                )
                for position in stock_positions
            )
            market_price = market_prices.get(symbol)
            current_value = self._finite_float_or_none(summary.get("current_value"))
            if current_value is None:
                current_value = reported_market_value
            current_shares = self._finite_float_or_none(summary.get("current_shares"))
            if current_shares is None:
                current_shares = live_shares
            symbol_config = self.config.portfolio.symbols[symbol]
            approved_buy_shares = rebalance_shares.get(symbol, 0)
            result[symbol] = {
                "configured_weight": float(symbol_config.weight),
                "primary_exchange": str(symbol_config.primary_exchange),
                "market_price": market_price,
                "current_shares": current_shares,
                "current_value": current_value,
                "broker_position": {
                    "shares": live_shares,
                    "market_value": reported_market_value,
                    "average_cost_per_share": (
                        weighted_cost / gross_shares if gross_shares > 0 else None
                    ),
                    "unrealized_pnl": reported_unrealized_pnl,
                    "realized_pnl": reported_realized_pnl,
                },
                "current_weight": self._finite_float_or_none(
                    summary.get("current_weight")
                ),
                "target_weight": self._finite_float_or_none(
                    summary.get("target_weight")
                ),
                "target_value": self._finite_float_or_none(summary.get("target_value")),
                "target_shares": self._finite_float_or_none(
                    summary.get("target_shares")
                ),
                "approved_buy_shares": approved_buy_shares,
                "approved_buy_value": (
                    approved_buy_shares * market_price
                    if market_price is not None
                    else None
                ),
                "tail_program": TailProgramInput.model_validate(
                    target, from_attributes=True
                ).model_dump(mode="json"),
                "target_modifiers": {
                    "volatility_weight": summary.get("volatility_weight"),
                    "target_weight_policy": summary.get("target_weight_policy"),
                    "absolute_trend": summary.get("absolute_trend"),
                },
            }
        return result

    def _tail_harvest_decision_fallback(
        self,
        *,
        policy: TailHarvestDecisionConfig,
        error: str,
    ) -> tuple[bool, dict[str, Any]]:
        if policy.on_error == "abort":
            raise RuntimeError(f"External tail harvest decision failed: {error}")
        harvest = policy.on_error == "baseline"
        status = "baseline" if harvest else "skipped"
        log.warning(
            f"External tail harvest decision failed; using {status} behavior ({error})."
        )
        return harvest, {
            "status": status,
            "harvest": harvest,
            "reason": None,
            "error": error,
            "provider": policy.provider,
        }

    async def _evaluate_tail_harvest_decision(
        self,
        snapshot: _TailHarvestSnapshot,
        *,
        rebalance_shares: dict[str, int],
        market_prices: dict[str, float],
        regime_summary: list[dict[str, Any]],
        history_cache: RegimeHistoryCache | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        tail_hedge = self.config.strategies.tail_hedge
        policy = getattr(tail_hedge, "harvest_decision", None)
        if policy is None or not getattr(policy, "enabled", False):
            return True, {}

        try:
            market_data_config = policy.market_data
            market_data = await self._load_external_market_data(
                market_data_config=market_data_config,
                strategy_symbols=(target.symbol for target in tail_hedge.targets),
                symbol_configs=self.config.portfolio.symbols,
                decision_name="tail harvest decision",
                history_cache=history_cache,
            )
            band_payload = self._tail_harvest_band_payload(
                snapshot,
                rebalance_shares=rebalance_shares,
                market_prices=market_prices,
            )
            request = build_tail_harvest_request(
                generated_at=self._as_utc(self._now()),
                dry_run=self.dry_run,
                tail_hedge=tail_hedge,
                regime_weight_base=self.config.strategies.regime_rebalance.weight_base.value,
                regime_margin_usage=AccountingPolicy.from_config(
                    self.config
                ).regime_margin_usage,
                account={
                    "net_liquidation": snapshot.net_liquidation,
                    "regime_rebalance_base": snapshot.regime_base,
                    "excluded_option_value": snapshot.excluded_option_value,
                },
                opportunity={
                    "sleeve_value": snapshot.band.sleeve_value,
                    "sleeve_weight": snapshot.band.sleeve_weight,
                    "harvest_trigger_weight": tail_hedge.harvest_trigger_weight,
                    "harvest_target_weight": tail_hedge.harvest_target_weight,
                    "target_sleeve_value": snapshot.band.target_value,
                    "sale_budget": snapshot.band.sale_budget,
                    "approved_rebalance_value": band_payload[
                        "approved_rebalance_value"
                    ],
                    "planned_sales": self._tail_harvest_planned_sales(snapshot),
                },
                underlyings=self._tail_harvest_underlying_inputs(
                    snapshot,
                    rebalance_shares=rebalance_shares,
                    market_prices=market_prices,
                    regime_summary=regime_summary,
                ),
                hedge_positions=self._tail_harvest_hedge_inputs(snapshot),
                market_data=market_data,
            )
            response = await self.external_decisions.decide(policy.provider, request)
            output = validate_tail_harvest_response(
                response,
                policy=policy,
                history_dates=market_data.sessions,
                now=self._as_utc(self._now()),
            )
            return output.harvest, {
                "status": "applied",
                "harvest": output.harvest,
                "reason": output.reason,
                "error": None,
                **external_decision_response_metadata(
                    response,
                    provider=policy.provider,
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return self._tail_harvest_decision_fallback(
                policy=policy,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _enqueue_tail_harvest(
        self,
        *,
        net_liquidation: float,
        rebalance_shares: dict[str, int],
        market_prices: dict[str, float],
        regime_summary: list[dict[str, Any]],
        cohorts: list[TailHedgeCohort],
        portfolio_positions: dict[str, list[PortfolioItem]],
        history_cache: RegimeHistoryCache | None = None,
    ) -> set[str]:
        if self._tail_state_store is None:
            return set()

        quoted_limit_prices = await self._quote_tail_puts(
            symbols=set(rebalance_shares),
            cohorts=cohorts,
            portfolio_positions=portfolio_positions,
        )

        # Quote requests yield to ib_async. Re-evaluate all host-owned safety
        # constraints from the current cache before consulting an external
        # decision provider.
        policy = self.config.strategies.tail_hedge.harvest_decision
        snapshot = self._build_tail_harvest_snapshot(
            net_liquidation=net_liquidation,
            rebalance_shares=rebalance_shares,
            market_prices=market_prices,
            quoted_limit_prices=quoted_limit_prices,
            record_unprofitable=not policy.enabled,
        )
        if snapshot is None:
            return set()

        if not snapshot.candidates:
            # The first snapshot suppresses this per-cohort telemetry while an
            # external policy is enabled so an approved result will not record
            # it twice. Restore the baseline event when there is nothing to ask.
            if policy.enabled:
                snapshot = self._build_tail_harvest_snapshot(
                    net_liquidation=net_liquidation,
                    rebalance_shares=rebalance_shares,
                    market_prices=market_prices,
                    quoted_limit_prices=quoted_limit_prices,
                    record_unprofitable=True,
                )
                if snapshot is None:
                    return set()
            band_payload = self._tail_harvest_band_payload(
                snapshot,
                rebalance_shares=rebalance_shares,
                market_prices=market_prices,
            )
            for symbol, shares in rebalance_shares.items():
                self._record_tail_harvest(
                    "no_eligible_cohort",
                    symbol=symbol,
                    rebalance_shares=shares,
                    sale_budget=snapshot.band.sale_budget,
                    **band_payload,
                )
            return set()

        should_harvest, external_decision = await self._evaluate_tail_harvest_decision(
            snapshot,
            rebalance_shares=rebalance_shares,
            market_prices=market_prices,
            regime_summary=regime_summary,
            history_cache=history_cache,
        )
        if should_harvest and policy.enabled:
            # The provider and market-history requests yield. Re-quote and
            # rebuild the snapshot before any state or order mutation.
            quoted_limit_prices = await self._quote_tail_puts(
                symbols=set(rebalance_shares),
                cohorts=snapshot.cohorts,
                portfolio_positions=snapshot.portfolio_positions,
            )
            snapshot = self._build_tail_harvest_snapshot(
                net_liquidation=net_liquidation,
                rebalance_shares=rebalance_shares,
                market_prices=market_prices,
                quoted_limit_prices=quoted_limit_prices,
                record_unprofitable=True,
            )
            if snapshot is None:
                return set()
            expires_at = external_decision.get("expires_at")
            try:
                validate_decision_expiry(
                    datetime.fromisoformat(expires_at) if expires_at else None,
                    now=self._as_utc(self._now()),
                    decision_name="tail harvest decision",
                )
            except ExternalDecisionError as exc:
                should_harvest, external_decision = (
                    self._tail_harvest_decision_fallback(policy=policy, error=str(exc))
                )

        if external_decision:
            band_payload = self._tail_harvest_band_payload(
                snapshot,
                rebalance_shares=rebalance_shares,
                market_prices=market_prices,
            )
            for symbol, shares in rebalance_shares.items():
                self._record_tail_harvest(
                    "external_policy_decision",
                    symbol=symbol,
                    rebalance_shares=shares,
                    sale_budget=snapshot.band.sale_budget,
                    external_decision=external_decision,
                    **band_payload,
                )
        if not should_harvest:
            log.notice(
                "External tail-harvest policy declined the eligible baseline "
                "harvest opportunity."
            )
            return set()

        candidates = snapshot.candidates
        state = snapshot.state
        decision = snapshot.band
        sale_budget = decision.sale_budget
        band_payload = self._tail_harvest_band_payload(
            snapshot,
            rebalance_shares=rebalance_shares,
            market_prices=market_prices,
        )
        if external_decision:
            band_payload["external_decision"] = external_decision

        selected = self._select_tail_harvest_candidates(candidates, sale_budget)

        if not selected:
            for symbol, shares in rebalance_shares.items():
                self._record_tail_harvest(
                    "no_eligible_cohort",
                    symbol=symbol,
                    rebalance_shares=shares,
                    sale_budget=sale_budget,
                    **band_payload,
                )
            return set()

        enqueued_at = self._now()
        planned: list[PlannedHarvest] = []
        for candidate, quantity in selected:
            symbol = candidate.contract.symbol
            con_id = candidate.contract.conId
            state_cohort = state.find_open(candidate.entry_id, con_id)
            if (
                state_cohort is None
                or state_cohort.symbol != symbol
                or state_cohort.status != "active"
                or state_cohort.quantity < quantity
                or state_cohort.has_pending_recovery
            ):
                return set()
            multiplier = float(candidate.contract.multiplier)
            minimum_profitable_price = (
                math.floor(
                    (
                        candidate.cost_basis_per_contract
                        + candidate.estimated_fee_per_contract
                    )
                    / multiplier
                    * 100
                )
                + 1
            ) / 100
            minimum_limit_price = minimum_harvest_limit_price(
                quoted_limit_price=candidate.limit_price,
                sleeve_value=decision.sleeve_value,
                trigger_value=decision.trigger_value,
                profitable_limit_price=minimum_profitable_price,
            )
            minimum_net_proceeds = round(
                minimum_limit_price * multiplier - candidate.estimated_fee_per_contract,
                2,
            )
            state_cohort.quantity = min(state_cohort.quantity, candidate.quantity)
            state_cohort.begin_recovery(
                quantity=quantity,
                proceeds_per_contract=minimum_net_proceeds,
                enqueued_at=enqueued_at,
            )
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
            setattr(order, TAIL_HEDGE_MIN_LIMIT_PRICE_ATTR, minimum_limit_price)
            planned.append(
                PlannedHarvest(
                    candidate=candidate,
                    quantity=quantity,
                    order=order,
                    minimum_limit_price=minimum_limit_price,
                    minimum_net_proceeds=minimum_net_proceeds,
                )
            )

        try:
            self._tail_state_store.save(state)
        except RuntimeError:
            log.error(
                "Unable to persist tail-harvest recovery intents; keeping the "
                "approved rebalance unchanged."
            )
            return set()

        enqueued_symbols: set[str] = set()
        for plan in planned:
            candidate = plan.candidate
            quantity = plan.quantity
            symbol = candidate.contract.symbol
            estimated_net_proceeds = quantity * candidate.net_proceeds_per_contract
            estimated_gross_proceeds = quantity * candidate.gross_proceeds_per_contract
            estimated_fees = quantity * candidate.estimated_fee_per_contract
            stock_price = market_prices[symbol]
            self.order_ops.enqueue_order(candidate.contract, plan.order)
            enqueued_symbols.add(symbol)
            self._record_tail_harvest(
                "harvest_enqueued",
                symbol=symbol,
                entry_id=candidate.entry_id,
                con_id=candidate.contract.conId,
                expiration=candidate.expiration,
                quantity=quantity,
                limit_price=candidate.limit_price,
                minimum_limit_price=plan.minimum_limit_price,
                minimum_net_proceeds_per_contract=plan.minimum_net_proceeds,
                cost_basis_per_contract=candidate.cost_basis_per_contract,
                gross_proceeds=estimated_gross_proceeds,
                estimated_fees=estimated_fees,
                net_proceeds=estimated_net_proceeds,
                stock_price=stock_price,
                rebalance_shares=rebalance_shares[symbol],
                profit_multiple=candidate.profit_multiple,
                sale_budget=sale_budget,
                **band_payload,
            )
            log.notice(
                f"{symbol}: Harvesting {quantity} earliest-expiring profitable "
                f"tail put(s) for about {dfmt(estimated_net_proceeds)} net of "
                "estimated fees before the approved hard rebalance."
            )
        return enqueued_symbols

    async def _apply_tail_harvest(
        self,
        *,
        orders: list[tuple[str, str, int]],
        net_liquidation: float,
        market_prices: dict[str, float],
        regime_summary: list[dict[str, Any]],
        hard_underweight_symbols: set[str],
        cohorts: list[TailHedgeCohort],
        history_cache: RegimeHistoryCache | None = None,
    ) -> list[tuple[str, str, int]]:
        targets = self._configured_tail_harvest_targets()
        if not targets or not cohorts or not hard_underweight_symbols:
            return orders

        # Cash is intentionally absent from this decision. A harvest is an
        # allocation response to a same-symbol hard-underweight buy, not an
        # attempt to infer deployable capital from IBKR cash accounting.
        # Re-materialize ib_async's fill-current portfolio cache after
        # preceding awaits; no broker refresh request is needed.
        account_number = self.config.runtime.account.number
        portfolio_positions = portfolio_positions_to_dict(
            self.ibkr.portfolio(account=account_number)
        )

        rebalance_shares: dict[str, int] = {}
        for symbol, _primary_exchange, quantity in orders:
            if (
                quantity <= 0
                or symbol not in targets
                or symbol not in hard_underweight_symbols
            ):
                continue
            rebalance_shares[symbol] = quantity

        if not rebalance_shares:
            return orders

        _unavailable_con_ids, blocked_symbols, blocked_reason = (
            self._tail_harvest_conflicts(cohorts)
        )

        # The band is portfolio-wide. Until every prior reduction is resolved,
        # its eventual sleeve impact is unknown and no new excess can be sized
        # safely—even for a different target symbol.
        if blocked_reason is not None:
            self._record_tail_harvest_blocked(
                rebalance_shares=rebalance_shares,
                blocked_symbols=blocked_symbols,
                reason=blocked_reason,
            )
            return orders

        current_net_liquidation = self._latest_net_liquidation(net_liquidation)
        current_regime_base, _ = self._regime_rebalance_base_value(
            net_liquidation=current_net_liquidation,
            portfolio_positions=portfolio_positions,
            market_prices=market_prices,
            cohorts=cohorts,
        )
        sleeve_value = self._tail_hedge_market_value(portfolio_positions, cohorts)
        tail_hedge = self.config.strategies.tail_hedge
        decision = evaluate_harvest_band(
            portfolio_base_value=current_regime_base,
            sleeve_value=sleeve_value,
            trigger_weight=tail_hedge.harvest_trigger_weight,
            target_weight=tail_hedge.harvest_target_weight,
        )
        if decision is None:
            return orders

        enqueued_symbols = await self._enqueue_tail_harvest(
            net_liquidation=current_net_liquidation,
            rebalance_shares=rebalance_shares,
            market_prices=market_prices,
            regime_summary=regime_summary,
            cohorts=cohorts,
            portfolio_positions=portfolio_positions,
            history_cache=history_cache,
        )
        summaries = {str(item["symbol"]): item for item in regime_summary}
        for symbol in enqueued_symbols:
            summary = summaries.get(symbol)
            if summary is not None:
                summary["action"] = (
                    f"[magenta]Harvest puts first; "
                    f"[green]buy {rebalance_shares[symbol]}"
                )

        # The stock orders are preliminary planning output. In live mode a
        # newly queued harvest executes first, then the full regime plan is
        # recalculated from refreshed broker state. Existing or unavailable
        # harvests never mutate an otherwise approved rebalance.
        return orders

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
    def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
        total_weight = sum(weights.values())
        if total_weight <= 0:
            raise ValueError("weights must sum to a positive value")
        return {symbol: weight / total_weight for symbol, weight in weights.items()}

    @staticmethod
    def _weighted_return_index(
        symbols: list[str],
        weights: dict[str, float],
        aligned_closes: dict[str, list[float]],
        length: int,
        eps: float = 0.0,
    ) -> list[float]:
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
    def _bars_to_closes(bars: Iterable[Any]) -> dict[date, float]:
        closes: dict[date, float] = {}
        for bar in bars:
            bar_date = bar.date.date() if hasattr(bar.date, "date") else bar.date
            closes[bar_date] = float(bar.close)
        return closes

    @staticmethod
    def _describe_history_closes(closes: dict[date, float]) -> str:
        if not closes:
            return "0 bars"
        sorted_dates = sorted(closes)
        return f"{len(sorted_dates)} bars {sorted_dates[0]}..{sorted_dates[-1]}"

    @classmethod
    def _align_regime_closes(
        cls,
        *,
        symbols: list[str],
        closes_by_symbol: ClosesBySymbol,
        required_points: int,
        required_dates: list[date],
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

        aligned_closes: dict[str, list[float]] = {}
        for symbol in symbols:
            aligned: list[float] = []
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
        symbols: list[str],
        dates: list[date],
        aligned_closes: dict[str, list[float]],
        ratio_gate: Any,
        effective_weights: dict[str, float],
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

    def get_primary_exchange(self, symbol: str) -> str:
        return self._get_primary_exchange(symbol)

    async def _get_regime_proxy_series(
        self,
        symbols: list[str],
        lookback_days: int,
        cooldown_days: int,
        weights_override: dict[str, float] | None = None,
        history_cache: RegimeHistoryCache | None = None,
    ) -> tuple[list[date], list[float]]:
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

    def _get_required_history_dates(self, required_points: int) -> list[date] | None:
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
        except Exception as exc:  # noqa: BLE001
            log.warning(
                f"Regime history freshness calculation failed ({type(exc).__name__})."
            )
            return None

    async def _fetch_regime_history_bars(
        self,
        symbol: str,
        duration: str,
        primary_exchange: str | None = None,
    ) -> tuple[str, list[Any]]:
        contract = Stock(
            symbol,
            self.order_ops.get_order_exchange(),
            currency="USD",
            primaryExchange=primary_exchange or self.get_primary_exchange(symbol),
        )
        for attempt in range(1, REGIME_HISTORY_MAX_ATTEMPTS + 1):
            if primary_exchange:
                bars = list(
                    await self.ibkr.request_historical_data(
                        contract,
                        duration,
                        cache_symbol=self._history_cache_symbol(
                            symbol, primary_exchange
                        ),
                    )
                )
            else:
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
        self,
        symbols: list[str],
        duration: str,
        primary_exchanges: dict[str, str] | None = None,
    ) -> ClosesBySymbol:
        tasks: list[Coroutine[Any, Any, tuple[str, list[Any]]]] = [
            self._fetch_regime_history_bars(
                symbol,
                duration,
                (primary_exchanges or {}).get(symbol),
            )
            for symbol in symbols
        ]
        histories = await log.track_async(
            tasks, description="Fetching regime rebalancing history..."
        )
        return {symbol: self._bars_to_closes(bars) for symbol, bars in histories}

    @staticmethod
    def _history_cache_symbol(symbol: str, primary_exchange: str | None) -> str:
        # The legacy cache is keyed by ticker alone. Explicit listing overrides
        # must neither consume nor overwrite that ticker's default history.
        return f"{symbol}@{primary_exchange}" if primary_exchange else symbol

    def _merge_cached_regime_closes(
        self,
        symbols: list[str],
        api_closes_by_symbol: ClosesBySymbol,
        required_dates: list[date],
        primary_exchanges: dict[str, str] | None = None,
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
                    self._history_cache_symbol(
                        symbol, (primary_exchanges or {}).get(symbol)
                    ),
                    REGIME_HISTORY_TIMEFRAME,
                    start_time,
                    end_time,
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
        symbols: list[str],
        api_closes_by_symbol: ClosesBySymbol,
        required_points: int,
        required_dates: list[date],
        primary_exchanges: dict[str, str] | None = None,
    ) -> AlignedClosesResult:
        merged_closes_by_symbol = self._merge_cached_regime_closes(
            symbols,
            api_closes_by_symbol,
            required_dates,
            primary_exchanges,
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
        symbols: list[str],
        lookback_days: int,
        cooldown_days: int,
        *,
        primary_exchanges: dict[str, str] | None = None,
    ) -> tuple[list[date], dict[str, list[float]]]:
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
        calendar_days = (self._now().date() - required_dates[0]).days + 1
        if calendar_days <= 0:
            raise RegimeHistoryValidationError(
                "Regime-aware rebalancing requires a valid history request window.",
                cache_recoverable=False,
            )
        duration = f"{calendar_days} D"
        api_closes_by_symbol = await self._fetch_regime_history_closes(
            symbols,
            duration,
            primary_exchanges,
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
                primary_exchanges=primary_exchanges,
            )

    async def _resolve_effective_weights(
        self,
        symbols: list[str],
        symbol_configs: dict[str, Any],
        history_cache: RegimeHistoryCache | None = None,
        *,
        exclude_current_run_state: bool = False,
    ) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        effective_weights = {
            symbol: float(symbol_configs[symbol].weight) for symbol in symbols
        }
        volatility_details: dict[str, dict[str, float]] = {}
        previous_state = (
            self.data_store.get_last_event_payload(
                "volatility_weight_state",
                exclude_current_run=exclude_current_run_state,
            )
            if self.data_store
            else None
        )
        previous_symbols = (
            previous_state.get("symbols", {})
            if isinstance(previous_state, dict)
            else {}
        )
        volatility_symbols_by_lookback: dict[int, list[str]] = {}

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
            except Exception as exc:  # noqa: BLE001
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
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        f"{symbol}: volatility weight calculation failed ({type(exc).__name__}); using static weight."
                    )

        return effective_weights, volatility_details

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        # datetime.astimezone() correctly interprets a naive value in the
        # process-local timezone. Replacing tzinfo would merely relabel local
        # wall time as UTC and could make expiry checks several hours late.
        return value.astimezone(UTC)

    async def _load_external_market_data(
        self,
        *,
        market_data_config: DecisionMarketDataConfig,
        strategy_symbols: Iterable[str],
        symbol_configs: dict[str, Any],
        decision_name: str,
        history_cache: RegimeHistoryCache | None = None,
    ) -> ExternalDecisionMarketData:
        history_symbols: list[str] = []
        if market_data_config.include_strategy_symbols:
            history_symbols.extend(strategy_symbols)
        history_symbols.extend(market_data_config.symbols)
        history_symbols = list(dict.fromkeys(history_symbols))
        if not history_symbols:
            raise ExternalDecisionError(
                f"{decision_name} market data universe is empty"
            )

        primary_exchanges: dict[str, str] = {}
        history_exchange_overrides: dict[str, str] = {}
        for symbol in history_symbols:
            market_symbol = market_data_config.symbols.get(symbol)
            explicit_exchange = (
                market_symbol.primary_exchange.strip()
                if market_symbol is not None
                else ""
            )
            configured_exchange = explicit_exchange
            if not configured_exchange and symbol in symbol_configs:
                configured_exchange = str(
                    symbol_configs[symbol].primary_exchange
                ).strip()
            if not configured_exchange:
                raise ExternalDecisionError(
                    f"{decision_name} market data symbol {symbol} does not have "
                    "a primary exchange"
                )
            primary_exchanges[symbol] = configured_exchange
            if explicit_exchange:
                history_exchange_overrides[symbol] = configured_exchange

        lookback_days = int(market_data_config.lookback_days)
        fetch_history: AlignedClosesFetcher = (
            self._get_regime_aligned_closes
            if history_cache is None
            else history_cache.get
        )
        history_dates, aligned_closes = await fetch_history(
            history_symbols,
            lookback_days,
            0,
            primary_exchanges=history_exchange_overrides or None,
        )
        return ExternalDecisionMarketData(
            timeframe=REGIME_HISTORY_TIMEFRAME,
            sessions=history_dates,
            closes=aligned_closes,
            primary_exchanges=primary_exchanges,
        )

    def _target_weight_policy_fallback(
        self,
        effective_weights: dict[str, float],
        *,
        policy: TargetWeightPolicyConfig,
        error: str,
    ) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
        if policy.on_error == "abort":
            raise RuntimeError(
                f"External target weight policy failed: {error}"
            ) from None
        log.warning(
            "External target weight policy failed; using post-volatility "
            f"baseline targets ({error})."
        )
        return (
            dict(effective_weights),
            {
                symbol: {
                    "status": "baseline",
                    "baseline_weight": effective_weights[symbol],
                    "multiplier": 1.0,
                    "raw_weight": effective_weights[symbol],
                    "effective_weight": effective_weights[symbol],
                    "reason": None,
                    "error": error,
                    "risk_ready": False,
                }
                for symbol in policy.symbols
            },
        )

    async def _apply_target_weight_policy(
        self,
        effective_weights: dict[str, float],
        *,
        symbols: list[str],
        symbol_configs: dict[str, Any],
        volatility_details: dict[str, dict[str, float]],
        account: BrokerAccountSnapshot,
        regime_margin_usage: float,
        total_value: float,
        excluded_value: float,
        last_rebalance: datetime | None,
        current_positions: dict[str, int],
        current_values: dict[str, float],
        market_prices: dict[str, float],
        history_cache: RegimeHistoryCache | None = None,
    ) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
        regime_rebalance = self.config.strategies.regime_rebalance
        policy = getattr(regime_rebalance, "target_weight_policy", None)
        if policy is None or not getattr(policy, "enabled", False):
            return dict(effective_weights), {}
        if (
            self._target_weight_policy_outcome is not None
            and self._target_weight_policy_outcome.response is not None
        ):
            try:
                validate_decision_expiry(
                    self._target_weight_policy_outcome.response.expires_at,
                    now=self._as_utc(self._now()),
                    decision_name="target weight policy",
                )
            except ExternalDecisionError as exc:
                self._target_weight_policy_outcome = _TargetWeightPolicyOutcome(
                    adjustments=None, response=None, error=str(exc)
                )

        if self._target_weight_policy_outcome is None:
            try:
                market_data = await self._load_external_market_data(
                    market_data_config=policy.market_data,
                    strategy_symbols=symbols,
                    symbol_configs=symbol_configs,
                    decision_name="target weight policy",
                    history_cache=history_cache,
                )
                request = build_target_weight_request(
                    generated_at=self._as_utc(self._now()),
                    dry_run=self.dry_run,
                    config=self.config,
                    effective_weights=effective_weights,
                    symbols=symbols,
                    symbol_configs=symbol_configs,
                    volatility_details=volatility_details,
                    account=account,
                    regime_margin_usage=regime_margin_usage,
                    total_value=total_value,
                    excluded_value=excluded_value,
                    last_rebalance=last_rebalance,
                    current_positions=current_positions,
                    current_values=current_values,
                    market_prices=market_prices,
                    market_data=market_data,
                )
                response = await self.external_decisions.decide(
                    policy.provider,
                    request,
                )
                adjustments = validate_target_weight_response(
                    response,
                    policy=policy,
                    history_dates=market_data.sessions,
                    now=self._as_utc(self._now()),
                )
                self._target_weight_policy_outcome = _TargetWeightPolicyOutcome(
                    adjustments=adjustments,
                    response=response,
                    error=None,
                )
            except Exception as exc:  # noqa: BLE001
                self._target_weight_policy_outcome = _TargetWeightPolicyOutcome(
                    adjustments=None,
                    response=None,
                    error=f"{type(exc).__name__}: {exc}",
                )

        outcome = self._target_weight_policy_outcome
        if (
            outcome.error is not None
            or outcome.adjustments is None
            or outcome.response is None
        ):
            return self._target_weight_policy_fallback(
                effective_weights,
                policy=policy,
                error=outcome.error or "provider returned no adjustments",
            )

        try:
            adjusted_weights, weight_details = apply_target_weight_adjustments(
                effective_weights,
                outcome.adjustments,
                policy=policy,
                volatility_bounds={
                    symbol: (
                        float(symbol_configs[symbol].volatility_weight.min_weight),
                        float(symbol_configs[symbol].volatility_weight.max_weight),
                    )
                    for symbol in outcome.adjustments
                    if policy.symbols[symbol].clamp_to_volatility_bounds
                },
                eps=regime_rebalance.eps,
            )
            response_metadata = external_decision_response_metadata(
                outcome.response, provider=policy.provider
            )
            details: dict[str, dict[str, Any]] = {
                symbol: {
                    "status": "applied",
                    **weight_details[symbol],
                    "multiplier": adjustment.multiplier,
                    "reason": adjustment.reason,
                    **response_metadata,
                    "risk_ready": True,
                }
                for symbol, adjustment in outcome.adjustments.items()
            }
        except (AttributeError, KeyError, TypeError, ExternalDecisionError) as exc:
            return self._target_weight_policy_fallback(
                effective_weights,
                policy=policy,
                error=f"{type(exc).__name__}: {exc}",
            )

        for symbol, detail in details.items():
            log.notice(
                f"{symbol}: external target policy provider={policy.provider} "
                f"producer={detail['producer']}@{detail['producer_version']} "
                f"multiplier={ffmt(detail['multiplier'])} "
                f"target={pfmt(detail['baseline_weight'])}->"
                f"{pfmt(detail['effective_weight'])}"
            )
        return adjusted_weights, details

    async def _apply_absolute_trend(
        self,
        effective_weights: dict[str, float],
        symbol_configs: dict[str, Any],
        history_cache: RegimeHistoryCache,
        *,
        exclude_current_run_state: bool = False,
    ) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
        adjusted_weights = dict(effective_weights)
        trend_details: dict[str, dict[str, Any]] = {}
        trend_configs: dict[str, Any] = {}
        trend_symbols_by_lookback: dict[int, list[str]] = {}

        for symbol in effective_weights:
            absolute_trend = getattr(symbol_configs[symbol], "absolute_trend", None)
            if absolute_trend is None or not getattr(absolute_trend, "enabled", False):
                continue
            trend_configs[symbol] = absolute_trend
            lookback_days = int(absolute_trend.lookback_days)
            trend_symbols_by_lookback.setdefault(lookback_days, []).append(symbol)

        if not trend_symbols_by_lookback:
            return adjusted_weights, trend_details

        previous_state = (
            self.data_store.get_last_event_payload(
                ABSOLUTE_TREND_STATE_EVENT,
                exclude_current_run=exclude_current_run_state,
                raise_on_error=True,
            )
            if self.data_store
            else None
        )
        previous_symbols_raw = (
            previous_state.get("symbols") if isinstance(previous_state, dict) else None
        )
        previous_symbols = (
            previous_symbols_raw if isinstance(previous_symbols_raw, dict) else {}
        )

        def apply_signal(
            symbol: str,
            signal: _AbsoluteTrendSignal,
            *,
            history_source: str,
            history_failure: str | None = None,
        ) -> dict[str, Any]:
            details = signal.target_details(
                pre_trend_target=adjusted_weights[symbol],
                risk_off_multiplier=float(trend_configs[symbol].risk_off_multiplier),
                history_source=history_source,
                history_failure=history_failure,
            )
            adjusted_weights[symbol] = float(details["final_target"])
            trend_details[symbol] = details
            return details

        def apply_persisted_state(
            symbol: str,
            lookback_days: int,
            failure_reason: str,
        ) -> None:
            try:
                signal = _AbsoluteTrendSignal.from_payload(
                    previous_symbols.get(symbol),
                    lookback_days=lookback_days,
                )
            except (TypeError, ValueError) as exc:
                log.error(
                    f"{symbol}: absolute trend history is unavailable and no "
                    "valid persisted state exists; aborting rebalancing."
                )
                raise RuntimeError(
                    f"{symbol}: absolute trend requires current history or a "
                    "valid persisted state."
                ) from exc

            details = apply_signal(
                symbol,
                signal,
                history_source="persisted",
                history_failure=failure_reason,
            )
            log.warning(
                f"{symbol}: absolute trend history unavailable "
                f"({failure_reason}); retaining persisted "
                f"{signal.state.replace('_', '-')} state with target "
                f"{pfmt(details['pre_trend_target'])}->"
                f"{pfmt(details['final_target'])}."
            )

        for lookback_days, group_symbols in trend_symbols_by_lookback.items():
            try:
                dates, aligned_closes = await history_cache.get(
                    group_symbols,
                    lookback_days,
                    0,
                )
            except Exception as exc:  # noqa: BLE001
                failure_reason = type(exc).__name__
                for symbol in group_symbols:
                    apply_persisted_state(symbol, lookback_days, failure_reason)
                continue

            for symbol in group_symbols:
                try:
                    signal = _AbsoluteTrendSignal.from_history(
                        dates=dates,
                        closes=aligned_closes[symbol],
                        lookback_days=lookback_days,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    apply_persisted_state(
                        symbol,
                        lookback_days,
                        type(exc).__name__,
                    )
                    continue

                details = apply_signal(symbol, signal, history_source="fresh")
                log.notice(
                    f"{symbol}: absolute trend latest={signal.latest_close:.4f} "
                    f"average_{lookback_days}d={signal.moving_average:.4f} "
                    f"return_{lookback_days}d={pfmt(signal.lookback_return)} "
                    f"state={signal.state.replace('_', '-')} "
                    f"multiplier={ffmt(details['applied_multiplier'])} "
                    f"target={pfmt(details['pre_trend_target'])}->"
                    f"{pfmt(details['final_target'])}"
                )

        return adjusted_weights, trend_details

    async def _get_last_regime_rebalance_time(
        self, symbols: list[str]
    ) -> datetime | None:
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
            except Exception as exc:  # noqa: BLE001
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
    ) -> datetime | None:
        symbols = set(symbols)
        last_rebalance: datetime | None = None
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
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Regime rebalancing cooldown calculation failed "
                f"({type(exc).__name__}); using calendar days."
            )
            return (end_date - start_date).days >= cooldown_days

    async def check_regime_rebalance_positions(
        self,
        account_summary: AccountSummary,
        portfolio_positions: dict[str, list[PortfolioItem]],
        *,
        exclude_current_run_state: bool = False,
    ) -> tuple[Table, list[tuple[str, str, int]]]:
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

        to_trade: list[tuple[str, str, int]] = []
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
        stock_symbols: dict[str, PortfolioItem] = {
            position.contract.symbol: position for position in stock_positions
        }

        async def get_ticker_task(symbol: str) -> tuple[str, Ticker]:
            ticker = await self.ibkr.get_ticker_for_stock(
                symbol, self.get_primary_exchange(symbol)
            )
            return symbol, ticker

        ticker_tasks: list[Coroutine[Any, Any, tuple[str, Ticker]]] = [
            get_ticker_task(symbol) for symbol in symbols
        ]
        ticker_results = await log.track_async(
            ticker_tasks, description="Fetching regime rebalancing prices..."
        )
        tickers = {symbol: ticker for symbol, ticker in ticker_results}

        current_positions: dict[str, int] = {}
        current_values: dict[str, float] = {}
        market_prices: dict[str, float] = {}
        target_shares: dict[str, int] = {}
        target_values: dict[str, float] = {}
        relative_drifts: dict[str, float] = {}
        share_gaps: dict[str, int] = {}
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

        weight_base = regime_rebalance.weight_base
        account = BrokerAccountSnapshot(account_summary)
        accounting_policy = AccountingPolicy.from_config(self.config)
        regime_margin_usage = accounting_policy.regime_margin_usage
        net_liq = account.net_liquidation
        total_value, excluded_value = self._regime_rebalance_base_value(
            net_liquidation=net_liq,
            portfolio_positions=portfolio_positions,
            market_prices=market_prices,
            cohorts=tail_cohorts,
        )
        if weight_base == RegimeRebalanceBaseEnum.net_liq_ex_options:
            log.notice(
                "Regime rebalancing base: mode=net_liq_ex_options "
                f"net_liq={dfmt(net_liq)} excluded_options={dfmt(excluded_value)} "
                f"margin_usage={ffmt(regime_margin_usage)} "
                f"base={dfmt(total_value)}"
            )
        elif weight_base == RegimeRebalanceBaseEnum.net_liq:
            log.notice(
                "Regime rebalancing base: mode=net_liq "
                f"net_liq={dfmt(net_liq)} "
                f"excluded_tail_hedges={dfmt(excluded_value)} "
                f"margin_usage={ffmt(regime_margin_usage)} "
                f"base={dfmt(total_value)}"
            )
        if total_value <= 0:
            log.error("Rebalance base value is not positive, skipping rebalancing.")
            raise ValueError("Regime-aware rebalancing requires a positive base value.")

        history_cache = RegimeHistoryCache(self._get_regime_aligned_closes)
        current_weights: dict[str, float] = {}
        effective_weights, volatility_details = await self._resolve_effective_weights(
            symbols,
            symbol_configs,
            history_cache,
            exclude_current_run_state=exclude_current_run_state,
        )
        post_volatility_effective_weights = dict(effective_weights)
        post_volatility_total_effective_weight = sum(
            post_volatility_effective_weights.values()
        )
        (
            effective_weights,
            target_weight_policy_details,
        ) = await self._apply_target_weight_policy(
            effective_weights,
            symbols=symbols,
            symbol_configs=symbol_configs,
            volatility_details=volatility_details,
            account=account,
            regime_margin_usage=regime_margin_usage,
            total_value=total_value,
            excluded_value=excluded_value,
            last_rebalance=last_rebalance,
            current_positions=current_positions,
            current_values=current_values,
            market_prices=market_prices,
            history_cache=history_cache,
        )
        pre_trend_effective_weights = dict(effective_weights)
        pre_trend_total_effective_weight = sum(pre_trend_effective_weights.values())
        effective_weights, trend_details = await self._apply_absolute_trend(
            effective_weights,
            symbol_configs,
            history_cache,
            exclude_current_run_state=exclude_current_run_state,
        )
        target_modifier_details = (
            ("volatility_weight", volatility_details),
            ("absolute_trend", trend_details),
        )
        target_policy = getattr(regime_rebalance, "target_weight_policy", None)
        harvest_risk_ready_symbols = {
            symbol
            for symbol in symbols
            if all(
                not bool(
                    getattr(
                        getattr(symbol_configs[symbol], config_name, None),
                        "enabled",
                        False,
                    )
                )
                or symbol in modifier_details
                for config_name, modifier_details in target_modifier_details
            )
            and (
                target_policy is None
                or not target_policy.enabled
                or symbol not in target_policy.symbols
                or bool(
                    target_weight_policy_details.get(symbol, {}).get(
                        "risk_ready", False
                    )
                )
            )
        }
        total_effective_weight = sum(effective_weights.values())
        if not math.isfinite(total_effective_weight) or total_effective_weight < 0:
            log.error("Regime-aware rebalancing effective weights are invalid.")
            raise ValueError(
                "Regime-aware rebalancing requires finite non-negative "
                "effective weights."
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
        stacked_target_weight = max(0.0, total_effective_weight - 1.0)
        if unallocated_target_weight > regime_rebalance.eps:
            log.notice(
                "Regime-aware rebalancing leaving "
                f"{pfmt(unallocated_target_weight)} outside managed targets for "
                "cash reserves."
            )
        elif stacked_target_weight > regime_rebalance.eps:
            volatility_increase_weight = sum(
                max(
                    0.0,
                    details["effective_weight"] - details["base_weight"],
                )
                for details in volatility_details.values()
            )
            target_policy_increase_weight = sum(
                max(
                    0.0,
                    details["effective_weight"] - details["baseline_weight"],
                )
                for details in target_weight_policy_details.values()
                if details.get("status") == "applied"
            )
            if stacked_target_weight > (
                volatility_increase_weight
                + target_policy_increase_weight
                + regime_rebalance.eps
            ):
                log.error(
                    "Regime-aware rebalancing effective weights exceed 100% "
                    "without a sufficient bounded dynamic-weight increase."
                )
                raise ValueError(
                    "Only volatility-adjusted weights or explicitly bounded external "
                    "weights may stack above 100%."
                )
            log.notice(
                "Regime-aware rebalancing stacking "
                f"{pfmt(stacked_target_weight)} above the configured 100% "
                "allocation using the NLV-backed margin base."
            )
        normalized_effective_weights = (
            {
                symbol: weight / total_effective_weight
                for symbol, weight in effective_weights.items()
            }
            if total_effective_weight > 0
            else {symbol: 0.0 for symbol in symbols}
        )
        for symbol in symbols:
            market_price = market_prices[symbol]
            current_position = current_positions[symbol]
            current_value = current_values[symbol]
            current_weights[symbol] = current_value / total_value
            target_weight = effective_weights[symbol]
            target_values[symbol] = target_weight * total_value
            target_shares[symbol] = math.floor(target_values[symbol] / market_price)
            share_gaps[symbol] = target_shares[symbol] - current_position
            if target_weight > 0:
                relative_drift = abs(current_weights[symbol] / target_weight - 1.0)
            elif math.isclose(
                current_weights[symbol],
                0.0,
                rel_tol=0.0,
                abs_tol=regime_rebalance.eps,
            ):
                relative_drift = 0.0
            else:
                # A zero target with a live position is a full hard-band drift.
                relative_drift = 1.0
            relative_drifts[symbol] = relative_drift

        invested_value = sum(current_values.values())
        proxy_symbols = [symbol for symbol in symbols if current_values[symbol] > 0]
        proxy_weights: dict[str, float] = {}
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
            # A zero trend multiplier can intentionally move every final target to
            # cash. Keep the market proxy usable with the post-volatility weights.
            proxy_weights = (
                effective_weights
                if total_effective_weight > 0
                else post_volatility_effective_weights
            )

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
        ratio_result: RatioGateResult | None = None
        if ratio_gate is not None:
            ratio_anchor = getattr(ratio_gate, "anchor", "")
            ratio_rest = [symbol for symbol in symbols if symbol != ratio_anchor]
            ratio_effective_weights = effective_weights
            if (
                ratio_rest
                and sum(effective_weights[symbol] for symbol in ratio_rest) <= 0
            ):
                # Preserve a usable relative-market signal when zero trend
                # multipliers intentionally move the entire rest basket to cash.
                ratio_effective_weights = post_volatility_effective_weights
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
                effective_weights=ratio_effective_weights,
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
            state = self.data_store.get_last_event_payload(
                "regime_rebalance_state",
                exclude_current_run=exclude_current_run_state,
            )
            if state:
                flow_was_active = bool(state.get("flow_active", False))
                deficit_was_active = bool(state.get("deficit_active", False))

        unallocated_rebalance_capacity = total_value - invested_value
        stacked_target_value = stacked_target_weight * total_value
        deficit_rebalance_capacity = (
            unallocated_rebalance_capacity + stacked_target_value
        )
        # Preserve ordinary positive inferred capacity, but do not classify the
        # authorized margin stack as a withdrawal or funding deficit.
        flow_rebalance_capacity = (
            unallocated_rebalance_capacity
            if unallocated_rebalance_capacity >= 0
            else min(0.0, deficit_rebalance_capacity)
        )
        flow_classification = (
            "inferred_capacity_deployment"
            if flow_rebalance_capacity > 0
            else "inferred_capacity_reduction"
            if flow_rebalance_capacity < 0
            else "none"
        )
        flow_trade_min_amount = total_value * regime_rebalance.flow_trade_min
        flow_trade_stop_amount = total_value * regime_rebalance.flow_trade_stop
        deficit_rail_start_amount = total_value * regime_rebalance.deficit_rail_start
        deficit_rail_stop_amount = total_value * regime_rebalance.deficit_rail_stop
        flow_gate = False
        deficit_gate = False
        if deficit_rebalance_capacity < 0:
            deficit_amount = -deficit_rebalance_capacity
            deficit_gate = deficit_amount >= deficit_rail_start_amount or (
                deficit_was_active and deficit_amount >= deficit_rail_stop_amount
            )
        if not deficit_gate:
            flow_amount = abs(flow_rebalance_capacity)
            flow_gate = flow_amount >= flow_trade_min_amount or (
                flow_was_active and flow_amount >= flow_trade_stop_amount
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
            flow_rebalance_capacity
        )
        if flow_gate and not flow_directional_imbalance_ok:
            flow_eligibility_gate_blockers.append("directional_imbalance")
        flow_eligibility_gates_ok = not flow_eligibility_gate_blockers
        flow_rebalance_eligible = flow_gate and flow_eligibility_gates_ok

        def build_flow_orders(amount: float) -> dict[str, int]:
            if amount == 0:
                return {}
            if not flow_candidate_symbols:
                return {}
            if flow_total_absolute_value_gap <= 0:
                return {}
            if not directional_flow_is_allowed(amount):
                return {}

            orders: dict[str, int] = {}
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
            shares_state: dict[str, int],
            amount: float,
            allow_below_target: bool,
            allowed_symbols: set[str],
        ) -> dict[str, int]:
            if amount <= 0:
                return {}
            orders: dict[str, int] = {}
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

        orders_by_symbol: dict[str, int] = {}
        rebalance_mode = "no"
        deficit_gate_after = False
        if hard_rebalance or soft_rebalance:
            rebalance_mode = "hard" if hard_rebalance else "soft"
            for symbol in symbols:
                desired = target_shares[symbol] - current_positions[symbol]
                if hard_rebalance and not math.isclose(rebalance_fraction, 1.0):
                    desired = round(desired * rebalance_fraction)
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
            excess_after = total_value + stacked_target_value - invested_after
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
                -deficit_rebalance_capacity - deficit_rail_stop_amount,
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
            flow_orders = build_flow_orders(flow_rebalance_capacity)
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
            if flow_rebalance_capacity > 0
            else "sell"
            if flow_rebalance_capacity < 0
            else "flat"
        )
        deficit_active_next = (
            deficit_gate_after if (hard_rebalance or soft_rebalance) else deficit_gate
        )
        flow_active_next = flow_gate and rebalance_mode in {"flow", "no"}

        regime_summary: list[dict[str, Any]] = []
        actionable_flow_buy_symbols: set[str] = set()
        net_liquidation_value = account.net_liquidation
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
            min_amount: float | None = None
            min_percent_relative: float | None = None

            if (
                filtered_trade_shares > 0
                and not allows_buy
                or filtered_trade_shares < 0
                and not allows_sell
            ):
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
            target_weight_policy_detail = target_weight_policy_details.get(symbol)
            trend_detail = trend_details.get(symbol)
            target_weight_display = pfmt(target_weight)
            if volatility_detail is not None:
                target_weight_display = (
                    f"{pfmt(target_weight)} "
                    f"(base {pfmt(volatility_detail['base_weight'])}, "
                    f"vol {pfmt(volatility_detail['realized_vol'])})"
                )
            if target_weight_policy_detail is not None:
                target_weight_display += (
                    " (external "
                    f"{target_weight_policy_detail['status']} "
                    f"x{ffmt(target_weight_policy_detail['multiplier'])})"
                )
            if trend_detail is not None:
                target_weight_display += (
                    f" (trend {trend_detail['state']} "
                    f"x{ffmt(trend_detail['applied_multiplier'])})"
                )

            summary_details = {
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
            if target_weight_policy_detail is not None:
                summary_details["target_weight_policy"] = target_weight_policy_detail
            if trend_detail is not None:
                summary_details["absolute_trend"] = trend_detail
            regime_summary.append(summary_details)

        to_trade = await self._apply_tail_harvest(
            orders=to_trade,
            net_liquidation=net_liquidation_value,
            market_prices=market_prices,
            regime_summary=regime_summary,
            hard_underweight_symbols=(
                hard_underweight_symbols & harvest_risk_ready_symbols
            ),
            cohorts=tail_cohorts,
            history_cache=history_cache,
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
            "flow_rebalance_capacity": flow_rebalance_capacity,
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
            f"flow_capacity={dfmt(flow_rebalance_capacity)} "
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
                    "stacked_target_weight": stacked_target_weight,
                    "stacked_target_value": stacked_target_value,
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
                    "stacked_target_weight": stacked_target_weight,
                    "stacked_target_value": stacked_target_value,
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
            if target_weight_policy_details:
                self.data_store.record_event(
                    TARGET_WEIGHT_POLICY_STATE_EVENT,
                    {
                        "post_volatility_total_effective_weight": (
                            post_volatility_total_effective_weight
                        ),
                        "post_policy_total_effective_weight": (
                            pre_trend_total_effective_weight
                        ),
                        "symbols": target_weight_policy_details,
                    },
                )
            if trend_details and not self.data_store.record_event(
                ABSOLUTE_TREND_STATE_EVENT,
                {
                    "pre_trend_total_effective_weight": (
                        pre_trend_total_effective_weight
                    ),
                    "final_total_effective_weight": total_effective_weight,
                    "symbols": trend_details,
                },
            ):
                raise RuntimeError("Failed to persist absolute trend state.")
            if volatility_details:
                volatility_unallocated_target_weight = max(
                    0.0, 1.0 - post_volatility_total_effective_weight
                )
                volatility_stacked_target_weight = max(
                    0.0, post_volatility_total_effective_weight - 1.0
                )
                self.data_store.record_event(
                    "volatility_weight_state",
                    {
                        "total_effective_weight": (
                            post_volatility_total_effective_weight
                        ),
                        "unallocated_target_weight": (
                            volatility_unallocated_target_weight
                        ),
                        "stacked_target_weight": volatility_stacked_target_weight,
                        "stacked_target_value": (
                            volatility_stacked_target_weight * total_value
                        ),
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
