from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from ib_async import PortfolioItem, Ticker, Trade, util
from ib_async.contract import Contract, Index, Option, Stock

from thetagang import log
from thetagang.config import Config
from thetagang.config_models import TailHedgeTargetConfig
from thetagang.db import DataStore
from thetagang.fmt import dfmt
from thetagang.ibkr import IBKR, RequiredFieldValidationError, TickerField
from thetagang.options import contract_date_to_datetime
from thetagang.trading_operations import OrderOperations
from thetagang.util import midpoint_or_market_price

TAIL_HEDGE_ENTRY_ORDER_REF = "tg:tail-hedge:entry"
TAIL_HEDGE_CLOSE_ORDER_REF = "tg:tail-hedge:close"
TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX = "tg:tail-harvest"
TAIL_HEDGE_EVALUATION_EVENT = "tail_hedge_evaluation"
TAIL_HEDGE_STATE_EVENT = "tail_hedge_state"
TAIL_HEDGE_STATE_SCHEMA_VERSION = 1
TAIL_HEDGE_STATE_STRATEGY = "long_put"
TAIL_HEDGE_ORDER_REFS = frozenset(
    {TAIL_HEDGE_ENTRY_ORDER_REF, TAIL_HEDGE_CLOSE_ORDER_REF}
)
TAIL_HEDGE_ACTIVE_HARVEST_STATUSES = frozenset(
    {
        "harvest_requested",
        "put_sell_working",
        "proceeds_realized",
        "rebalance_credit_ready",
        "stock_buy_enqueued",
    }
)
TAIL_HEDGE_ERRORS = (
    IndexError,
    RequiredFieldValidationError,
    RuntimeError,
    StopIteration,
    TypeError,
    ValueError,
)


@dataclass(frozen=True)
class PutQuote:
    expiration: str
    dte: int
    underlying_price: float
    con_id: int
    local_symbol: str
    strike: float
    bid: float
    ask: float
    open_interest: float
    midpoint: float
    limit_price: float
    premium_ratio: float
    bid_ask_ratio: float


@dataclass(frozen=True)
class UnderlyingQuote:
    contract: Contract
    price: float


@dataclass(frozen=True)
class WorkingTailOrder:
    broker_order: Any
    order_ref: str
    order_id: Optional[int]
    symbol: Optional[str]
    con_id: Optional[int]
    status: Optional[str]
    filled: Optional[float]
    remaining: Optional[float]
    limit_price: Optional[float]

    def event_payload(self) -> Dict[str, Any]:
        return {
            "order_ref": self.order_ref,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "con_id": self.con_id,
            "status": self.status,
            "filled": self.filled,
            "remaining": self.remaining,
            "limit_price": self.limit_price,
        }


class NoLaterExpirationError(RuntimeError):
    """Raised when a target's put ladder cannot extend to a later expiration."""


def tail_hedge_owned_con_ids(
    state: Optional[Dict[str, Any]],
    *,
    account_number: str,
) -> set[int]:
    """Return every contract ID owned by the current portfolio-level state."""
    if state is None:
        return set()
    _validate_state_identity(state, account_number)

    contract_ids: set[int] = set()
    tranches = state.get("tranches")
    if not isinstance(tranches, list):
        raise RuntimeError("Tail-hedge state has invalid tranche data")
    for tranche in tranches:
        if not isinstance(tranche, dict):
            raise RuntimeError("Tail-hedge state contains an invalid tranche")
        con_id = tranche.get("con_id")
        if not (
            isinstance(tranche.get("symbol"), str)
            and tranche["symbol"]
            and isinstance(tranche.get("entry_id"), str)
            and tranche["entry_id"]
            and isinstance(tranche.get("expiration"), str)
            and tranche["expiration"]
            and type(con_id) is int
            and con_id > 0
        ):
            raise RuntimeError("Tail-hedge state contains an invalid tranche")
        contract_ids.add(con_id)
    return contract_ids


def tail_hedge_state_collections(
    state: Optional[Dict[str, Any]],
    *,
    account_number: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return mutable copies of the three persisted tail-hedge collections."""
    if state is None:
        return [], [], []
    _validate_state_identity(state, account_number)

    collections: list[list[dict[str, Any]]] = []
    for key in ("tranches", "entry_history", "harvest_plans"):
        raw_items = state.get(key)
        if not isinstance(raw_items, list) or not all(
            isinstance(item, dict) for item in raw_items
        ):
            raise RuntimeError(f"Tail-hedge state has invalid {key} data")
        collections.append([dict(item) for item in raw_items])
    return collections[0], collections[1], collections[2]


def active_tail_harvest_con_ids(harvest_plans: List[Dict[str, Any]]) -> set[int]:
    return {
        int(sale["con_id"])
        for plan in harvest_plans
        if plan.get("status") in TAIL_HEDGE_ACTIVE_HARVEST_STATUSES
        for sale in plan.get("put_sales", [])
        if isinstance(sale, dict)
        and type(sale.get("con_id")) is int
        and sale["con_id"] > 0
    }


def active_tail_harvest_symbols(harvest_plans: List[Dict[str, Any]]) -> set[str]:
    return {
        str(plan["symbol"])
        for plan in harvest_plans
        if plan.get("status") in TAIL_HEDGE_ACTIVE_HARVEST_STATUSES
        and isinstance(plan.get("symbol"), str)
        and plan["symbol"]
    }


def _validate_state_identity(state: Dict[str, Any], account_number: str) -> None:
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != TAIL_HEDGE_STATE_SCHEMA_VERSION
        or state.get("strategy") != TAIL_HEDGE_STATE_STRATEGY
    ):
        raise RuntimeError("Tail-hedge state has an invalid schema")
    if state.get("account") != account_number:
        raise RuntimeError("Tail-hedge state belongs to a different account")


class TailHedgeEngine:
    """Maintain independent long-put ladders under one portfolio budget."""

    def __init__(
        self,
        *,
        config: Config,
        ibkr: IBKR,
        order_ops: OrderOperations,
        data_store: Optional[DataStore],
        now_provider: Callable[[], datetime] = datetime.now,
        get_regime_buy_symbols: Callable[[], set[str]] | None = None,
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.order_ops = order_ops
        self.data_store = data_store
        self._now = now_provider
        self._get_regime_buy_symbols = get_regime_buy_symbols
        self._cached_vix: Optional[float] = None

    async def manage(
        self,
        portfolio_positions: Dict[str, List[PortfolioItem]],
        *,
        net_liquidation: float,
    ) -> None:
        tail_config = self.config.strategies.tail_hedge
        if not tail_config.enabled:
            log.warning("Tail hedge not enabled, skipping...")
            return
        if self.data_store is None:
            raise RuntimeError("Tail hedge requires SQLite state storage.")

        log.notice("Evaluating tail-hedge long-put program...")
        self._cached_vix = None
        try:
            await self._manage_program(
                portfolio_positions,
                net_liquidation=net_liquidation,
            )
        except TAIL_HEDGE_ERRORS as exc:
            self._record_evaluation(
                "evaluation_error",
                error_type=type(exc).__name__,
                detail=str(exc),
            )
            log.error(f"Tail-hedge evaluation failed ({type(exc).__name__}): {exc}")

    async def _manage_program(
        self,
        portfolio_positions: Dict[str, List[PortfolioItem]],
        *,
        net_liquidation: float,
    ) -> None:
        if self.data_store is None:
            raise RuntimeError("Tail hedge requires SQLite state storage.")

        state = self.data_store.get_last_event_payload(
            TAIL_HEDGE_STATE_EVENT,
            raise_on_error=True,
        )
        tranches, entry_history, harvest_plans = self._state_parts(state)
        active_harvest_con_ids = active_tail_harvest_con_ids(harvest_plans)
        active_harvest_symbols = active_tail_harvest_symbols(harvest_plans)
        put_positions = self._put_positions_by_con_id(portfolio_positions)
        open_trades = self._account_open_trades()
        working_orders = self._working_tail_orders(open_trades)
        working_entry_orders = [
            order
            for order in working_orders
            if order.order_ref == TAIL_HEDGE_ENTRY_ORDER_REF
        ]
        working_close_orders = [
            order
            for order in working_orders
            if order.order_ref == TAIL_HEDGE_CLOSE_ORDER_REF
        ]
        working_close_con_ids = {
            order.con_id
            for order in working_close_orders
            if type(order.con_id) is int and order.con_id > 0
        }
        targets = {
            target.symbol: target
            for target in self.config.strategies.tail_hedge.targets
        }
        for working_order in working_entry_orders:
            if working_order.symbol in targets:
                continue
            try:
                self.ibkr.cancel_order(working_order.broker_order)
            except TAIL_HEDGE_ERRORS as exc:
                self._record_evaluation(
                    "entry_cancel_error",
                    symbol=working_order.symbol,
                    order=working_order.event_payload(),
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
                log.error(
                    "Failed to cancel an entry for a removed tail-hedge target "
                    f"({type(exc).__name__}): {exc}"
                )
            else:
                self._record_evaluation(
                    "entry_cancel_requested",
                    symbol=working_order.symbol,
                    order=working_order.event_payload(),
                )

        state_changed = False
        removed_entry_ids: list[str] = []
        history_ids_to_remove: set[str] = set()
        reconciled_tranches: list[dict[str, Any]] = []
        for tranche in tranches:
            con_id = int(tranche["con_id"])
            symbol = str(tranche["symbol"])
            position = put_positions.get(con_id)
            if position is None:
                matching_entries = [
                    order for order in working_entry_orders if order.symbol == symbol
                ]
                matching_con_ids = {
                    order.con_id
                    for order in matching_entries
                    if type(order.con_id) is int and order.con_id > 0
                }
                if (
                    tranche.get("status") == "entry_enqueued"
                    and matching_entries
                    and (not matching_con_ids or con_id in matching_con_ids)
                ):
                    reconciled_tranches.append(tranche)
                    continue
                entry_id = str(tranche["entry_id"])
                removed_entry_ids.append(entry_id)
                if tranche.get("status") == "entry_enqueued":
                    history_ids_to_remove.add(entry_id)
                state_changed = True
                continue

            reconciled = dict(tranche)
            if float(position.position) > 0 and reconciled.get("status") == (
                "entry_enqueued"
            ):
                reconciled["status"] = "active"
                state_changed = True
            position_quantity = self._position_quantity(position)
            if (
                float(position.position) > 0
                and reconciled.get("quantity") != position_quantity
            ):
                reconciled["quantity"] = position_quantity
                state_changed = True
            reconciled_tranches.append(reconciled)

        tranches[:] = reconciled_tranches
        if history_ids_to_remove:
            entry_history[:] = [
                entry
                for entry in entry_history
                if str(entry.get("entry_id")) not in history_ids_to_remove
            ]
        if state_changed:
            self._record_state(
                "reconciled",
                tranches=tranches,
                entry_history=entry_history,
                harvest_plans=harvest_plans,
                removed_entry_ids=removed_entry_ids,
                persistence_required=False,
            )

        blocked_entry_symbols = {
            str(order.symbol)
            for order in working_close_orders
            if isinstance(order.symbol, str)
        }
        blocked_entry_symbols.update(active_harvest_symbols)
        same_run_regime_buy_symbols = self._same_run_regime_buy_symbols()
        blocked_entry_symbols.update(same_run_regime_buy_symbols)
        for symbol in sorted(same_run_regime_buy_symbols & set(targets)):
            self._record_evaluation(
                "regime_buy_approved",
                symbol=symbol,
            )

        # Finish risk-reducing management for every symbol before evaluating
        # any new entry. Failures remain isolated to the affected target.
        for tranche_index, tranche in enumerate(tranches):
            con_id = int(tranche["con_id"])
            symbol = str(tranche["symbol"])
            position = put_positions.get(con_id)
            if position is None:
                continue
            if con_id in active_harvest_con_ids:
                blocked_entry_symbols.add(symbol)
                self._record_evaluation(
                    "harvest_plan_active",
                    symbol=symbol,
                    entry_id=tranche["entry_id"],
                    con_id=con_id,
                )
                continue
            if not self.config.trading_is_allowed(symbol):
                blocked_entry_symbols.add(symbol)
                self._record_evaluation(
                    "trading_disabled",
                    symbol=symbol,
                    entry_id=tranche["entry_id"],
                    con_id=con_id,
                )
                continue
            if con_id in working_close_con_ids:
                self._record_evaluation(
                    "working_close_order_present",
                    symbol=symbol,
                    entry_id=tranche["entry_id"],
                    con_id=con_id,
                )
                blocked_entry_symbols.add(symbol)
                continue
            try:
                close_enqueued = await self._manage_existing_put(
                    position,
                    targets.get(symbol),
                    tranches,
                    entry_history,
                    harvest_plans,
                    tranche_index,
                )
            except TAIL_HEDGE_ERRORS as exc:
                blocked_entry_symbols.add(symbol)
                self._record_evaluation(
                    "evaluation_error",
                    symbol=symbol,
                    entry_id=tranche["entry_id"],
                    con_id=con_id,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
                log.error(
                    f"{symbol}: Tail-put management failed "
                    f"({type(exc).__name__}): {exc}"
                )
                continue
            if close_enqueued:
                blocked_entry_symbols.add(symbol)

        occupied_con_ids = (
            set(put_positions)
            | self._queued_put_con_ids()
            | self._working_put_con_ids(open_trades)
        )
        for target in self.config.strategies.tail_hedge.targets:
            symbol = target.symbol
            if symbol in blocked_entry_symbols:
                continue
            symbol_working_entries = [
                order for order in working_entry_orders if order.symbol == symbol
            ]
            if symbol_working_entries:
                self._record_evaluation(
                    "working_order_present",
                    symbol=symbol,
                    orders=[order.event_payload() for order in symbol_working_entries],
                )
                log.notice(f"{symbol}: Tail-hedge entry order is working; holding.")
                continue
            try:
                await self._evaluate_entry(
                    target,
                    self._stock_exposure(portfolio_positions.get(symbol, [])),
                    net_liquidation=net_liquidation,
                    tranches=tranches,
                    entry_history=entry_history,
                    harvest_plans=harvest_plans,
                    occupied_con_ids=occupied_con_ids,
                )
            except TAIL_HEDGE_ERRORS as exc:
                self._record_evaluation(
                    "evaluation_error",
                    symbol=symbol,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
                log.error(
                    f"{symbol}: Tail-hedge entry evaluation failed "
                    f"({type(exc).__name__}): {exc}"
                )

    async def _manage_existing_put(
        self,
        position: PortfolioItem,
        target: Optional[TailHedgeTargetConfig],
        tranches: List[Dict[str, Any]],
        entry_history: List[Dict[str, Any]],
        harvest_plans: List[Dict[str, Any]],
        tranche_index: int,
    ) -> bool:
        tranche = tranches[tranche_index]
        symbol = str(tranche["symbol"])
        if position.contract.symbol != symbol:
            raise RuntimeError(
                f"Owned contract symbol {position.contract.symbol} does not match "
                f"state symbol {symbol}"
            )

        quantity = self._position_quantity(position)
        if float(position.position) < 0:
            await self._close_position(
                position,
                tranches,
                entry_history,
                harvest_plans,
                tranche_index,
                action="BUY",
                quantity=quantity,
                close_reason="owned_put_is_short",
            )
            return True
        if target is None:
            await self._close_position(
                position,
                tranches,
                entry_history,
                harvest_plans,
                tranche_index,
                action="SELL",
                quantity=quantity,
                close_reason="target_removed",
            )
            return True

        expiration = position.contract.lastTradeDateOrContractMonth
        dte = self._dte(expiration)
        if dte <= target.exit_dte:
            await self._close_position(
                position,
                tranches,
                entry_history,
                harvest_plans,
                tranche_index,
                action="SELL",
                quantity=quantity,
                close_reason="roll_dte",
            )
            return True

        self._record_evaluation(
            "long_put_held",
            symbol=symbol,
            entry_id=tranche["entry_id"],
            con_id=position.contract.conId,
            expiration=expiration,
            dte=dte,
            strike=float(position.contract.strike),
            quantity=quantity,
            market_value=float(getattr(position, "marketValue", 0.0) or 0.0),
        )
        return False

    async def _close_position(
        self,
        position: PortfolioItem,
        tranches: List[Dict[str, Any]],
        entry_history: List[Dict[str, Any]],
        harvest_plans: List[Dict[str, Any]],
        tranche_index: int,
        *,
        action: str,
        quantity: int,
        close_reason: str,
    ) -> None:
        symbol = str(tranches[tranche_index]["symbol"])
        position.contract.exchange = self.order_ops.get_order_exchange()
        ticker = await self._option_ticker(position.contract)
        limit_price = round(max(self._midpoint(ticker), 0.01), 2)
        order = self.order_ops.create_limit_order(
            action=action,
            quantity=quantity,
            limit_price=limit_price,
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
            transmit=True,
        )
        self.order_ops.enqueue_order(position.contract, order)

        updated = dict(tranches[tranche_index])
        updated.update(
            status="close_enqueued",
            close_reason=close_reason,
            close_limit_price=limit_price,
            close_enqueued_at=self._now(),
        )
        tranches[tranche_index] = updated
        self._record_state(
            "close_enqueued",
            tranches=tranches,
            entry_history=entry_history,
            harvest_plans=harvest_plans,
            action_symbol=symbol,
            action_entry_id=updated["entry_id"],
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
            persistence_required=False,
        )
        self._record_evaluation(
            "close_enqueued",
            symbol=symbol,
            entry_id=updated["entry_id"],
            con_id=position.contract.conId,
            expiration=position.contract.lastTradeDateOrContractMonth,
            dte=self._dte(position.contract.lastTradeDateOrContractMonth),
            quantity=quantity,
            action=action,
            limit_price=limit_price,
            close_reason=close_reason,
        )

    async def _evaluate_entry(
        self,
        target: TailHedgeTargetConfig,
        stock_exposure: float,
        *,
        net_liquidation: float,
        tranches: List[Dict[str, Any]],
        entry_history: List[Dict[str, Any]],
        harvest_plans: List[Dict[str, Any]],
        occupied_con_ids: set[int],
    ) -> None:
        if self.data_store is None:
            raise RuntimeError("Tail hedge requires SQLite state storage.")

        symbol = target.symbol
        if not self.config.trading_is_allowed(symbol):
            self._record_evaluation("trading_disabled", symbol=symbol)
            return
        if stock_exposure <= 0:
            self._record_evaluation("no_protected_stock_position", symbol=symbol)
            return
        if not self._is_positive(net_liquidation):
            raise RuntimeError("Net liquidation value is unavailable")

        now = self._now()
        budget_start = now - timedelta(days=365)
        recent_history = [
            entry
            for entry in entry_history
            if (entered_at := self._parse_datetime(entry.get("entered_at"))) is not None
            and entered_at >= budget_start
        ]
        target_history = [
            entry for entry in recent_history if entry.get("symbol") == symbol
        ]
        global_spent = sum(
            float(entry.get("estimated_cost", 0.0) or 0.0) for entry in recent_history
        )
        target_spent = sum(
            float(entry.get("estimated_cost", 0.0) or 0.0) for entry in target_history
        )
        annual_budget = round(
            net_liquidation * float(self.config.strategies.tail_hedge.annual_budget),
            2,
        )
        target_annual_budget = round(annual_budget * target.budget_weight, 2)
        remaining_budget = round(max(0.0, annual_budget - global_spent), 2)
        target_remaining_budget = round(
            max(0.0, target_annual_budget - target_spent),
            2,
        )
        tranche_budget = round(
            target_annual_budget / target.annual_tranches,
            2,
        )
        applicable_budget = round(
            min(
                remaining_budget,
                target_remaining_budget,
                tranche_budget,
            ),
            2,
        )
        budget_details = {
            "net_liquidation": net_liquidation,
            "protected_position_value": stock_exposure,
            "annual_budget_rate": float(
                self.config.strategies.tail_hedge.annual_budget
            ),
            "annual_budget": annual_budget,
            "budget_weight": float(target.budget_weight),
            "target_annual_budget": target_annual_budget,
            "annual_tranches": target.annual_tranches,
            "tranche_budget": tranche_budget,
            "applicable_budget": applicable_budget,
            "global_entry_spend": global_spent,
            "target_entry_spend": target_spent,
            "remaining_budget": remaining_budget,
            "target_remaining_budget": target_remaining_budget,
            "budget_window_start": budget_start,
        }
        if len(target_history) >= target.annual_tranches:
            self._record_evaluation(
                "annual_tranche_limit",
                symbol=symbol,
                **budget_details,
            )
            return

        latest_entry_at = max(
            (
                entered_at
                for entry in target_history
                if (entered_at := self._parse_datetime(entry.get("entered_at")))
                is not None
            ),
            default=None,
        )
        if latest_entry_at is not None:
            days_since_entry = (now - latest_entry_at).days
            if days_since_entry < target.tranche_interval_days:
                self._record_evaluation(
                    "tranche_entry_spacing",
                    symbol=symbol,
                    days_since_entry=days_since_entry,
                    tranche_interval_days=target.tranche_interval_days,
                    **budget_details,
                )
                return

        vix: Optional[float] = None
        if target.entry_gate == "vix":
            vix = await self._vix_price()
            if vix > target.entry_vix_max:
                self._record_evaluation(
                    "vix_above_entry_max",
                    symbol=symbol,
                    vix=vix,
                    entry_vix_max=target.entry_vix_max,
                    **budget_details,
                )
                return

        later_than_expiration = max(
            (
                str(tranche["expiration"])
                for tranche in tranches
                if tranche.get("symbol") == symbol
            ),
            default=None,
        )
        try:
            quote, contract = await self._find_put(
                target,
                later_than_expiration=later_than_expiration,
                exclude_con_ids=occupied_con_ids,
            )
        except NoLaterExpirationError:
            self._record_evaluation(
                "no_later_expiration_available",
                symbol=symbol,
                vix=vix,
                later_than_expiration=later_than_expiration,
                **budget_details,
            )
            return

        quote_details = {"vix": vix, "quote": asdict(quote), **budget_details}
        rejection = self._quote_rejection(target, quote)
        if rejection is not None:
            self._record_evaluation(rejection, symbol=symbol, **quote_details)
            return

        per_contract_cost = round(quote.limit_price * self._multiplier(contract), 2)
        budget_quantity = math.floor(applicable_budget / per_contract_cost)
        quantity = max(1, budget_quantity)
        entry_cost = round(per_contract_cost * quantity, 2)
        minimum_contract_floor_applied = budget_quantity < 1
        tranche_budget_overrun = round(max(0.0, entry_cost - tranche_budget), 2)
        target_annual_budget_overrun = round(
            max(0.0, target_spent + entry_cost - target_annual_budget),
            2,
        )
        global_annual_budget_overrun = round(
            max(0.0, global_spent + entry_cost - annual_budget),
            2,
        )
        sizing_details = {
            "minimum_contract_floor_applied": minimum_contract_floor_applied,
            "tranche_budget_overrun": tranche_budget_overrun,
            "target_annual_budget_overrun": target_annual_budget_overrun,
            "global_annual_budget_overrun": global_annual_budget_overrun,
        }
        entered_at = self._now()
        entry_id = f"{symbol}:{quote.con_id}:{entered_at.isoformat()}"
        tranche = {
            "entry_id": entry_id,
            "symbol": symbol,
            "status": "entry_enqueued",
            "con_id": quote.con_id,
            "local_symbol": quote.local_symbol,
            "expiration": quote.expiration,
            "strike": quote.strike,
            "quantity": quantity,
            "entry_limit_price": quote.limit_price,
            "entry_cost": entry_cost,
            "entry_enqueued_at": entered_at,
            **sizing_details,
        }
        history_entry = {
            "entry_id": entry_id,
            "symbol": symbol,
            "entered_at": entered_at,
            "estimated_cost": entry_cost,
            **sizing_details,
        }
        updated_tranches = [*tranches, tranche]
        active_entry_ids = {str(active["entry_id"]) for active in tranches}
        retained_history = [
            entry
            for entry in entry_history
            if str(entry.get("entry_id")) in active_entry_ids
            or (
                (entered_at := self._parse_datetime(entry.get("entered_at")))
                is not None
                and entered_at >= budget_start
            )
        ]
        updated_history = [*retained_history, history_entry]
        self._record_state(
            "entry_enqueued",
            tranches=updated_tranches,
            entry_history=updated_history,
            harvest_plans=harvest_plans,
            action_symbol=symbol,
            action_entry_id=entry_id,
            order_ref=TAIL_HEDGE_ENTRY_ORDER_REF,
        )
        tranches[:] = updated_tranches
        entry_history[:] = updated_history
        occupied_con_ids.add(quote.con_id)

        order = self.order_ops.create_limit_order(
            action="BUY",
            quantity=quantity,
            limit_price=quote.limit_price,
            use_default_algo=False,
            order_ref=TAIL_HEDGE_ENTRY_ORDER_REF,
            transmit=True,
        )
        self.order_ops.enqueue_order(contract, order)
        self._record_evaluation(
            "entry_enqueued",
            symbol=symbol,
            entry_id=entry_id,
            quantity=quantity,
            per_contract_cost=per_contract_cost,
            entry_cost=entry_cost,
            later_than_expiration=later_than_expiration,
            **sizing_details,
            **quote_details,
        )
        if minimum_contract_floor_applied and any(
            (
                tranche_budget_overrun,
                target_annual_budget_overrun,
                global_annual_budget_overrun,
            )
        ):
            log.warning(
                f"{symbol}: One-contract tail-hedge minimum exceeded a budget "
                f"(tranche={dfmt(tranche_budget_overrun)}, "
                f"target_annual={dfmt(target_annual_budget_overrun)}, "
                f"global_annual={dfmt(global_annual_budget_overrun)})."
            )
        log.notice(
            f"{symbol}: Enqueued {quantity}x {quote.strike:g} puts expiring "
            f"{quote.expiration} at {dfmt(quote.limit_price)} each."
        )

    @staticmethod
    def _quote_rejection(
        target: TailHedgeTargetConfig,
        quote: PutQuote,
    ) -> Optional[str]:
        if quote.open_interest < target.minimum_open_interest:
            return "insufficient_open_interest"
        if quote.bid < target.minimum_bid:
            return "bid_below_minimum"
        if quote.bid_ask_ratio > target.max_bid_ask_ratio:
            return "bid_ask_too_wide"
        if quote.premium_ratio > target.max_premium_ratio:
            return "put_too_expensive"
        return None

    async def _find_put(
        self,
        target: TailHedgeTargetConfig,
        *,
        later_than_expiration: Optional[str],
        exclude_con_ids: set[int],
    ) -> tuple[PutQuote, Contract]:
        symbol = target.symbol
        exchange = self.order_ops.get_order_exchange()
        underlying = await self._underlying_quote(target)

        chains = await self.ibkr.get_chains_for_contract(underlying.contract)
        matching_chains = [chain for chain in chains if chain.tradingClass == symbol]
        if not matching_chains:
            raise RuntimeError("No matching option chain is available")
        chain = next(
            (chain for chain in matching_chains if chain.exchange == exchange),
            matching_chains[0],
        )
        eligible_expirations = [
            (expiration, self._dte(expiration))
            for expiration in chain.expirations
            if target.min_dte <= self._dte(expiration) <= target.max_dte
            and (later_than_expiration is None or expiration > later_than_expiration)
        ]
        if not eligible_expirations:
            if later_than_expiration is not None:
                raise NoLaterExpirationError(
                    "No option expiration is inside the configured DTE range "
                    f"later than {later_than_expiration}"
                )
            raise RuntimeError(
                "No option expiration is inside the configured DTE range"
            )
        eligible_expirations.sort(
            key=lambda value: (
                abs(value[1] - target.target_dte),
                -value[1],
            )
        )

        strike_target = underlying.price * target.strike_ratio
        otm_strikes = [
            float(strike)
            for strike in chain.strikes
            if 0 < float(strike) < underlying.price
        ]
        if not otm_strikes:
            raise RuntimeError("No out-of-the-money put strikes are available")
        candidate_strikes = sorted(
            otm_strikes,
            key=lambda strike: abs(strike - strike_target),
        )[:5]
        contracts = await self.ibkr.qualify_contracts(
            *[
                Option(
                    symbol,
                    expiration,
                    strike,
                    "P",
                    exchange,
                    currency="USD",
                )
                for expiration, _dte in eligible_expirations
                for strike in candidate_strikes
            ]
        )
        contracts = [
            contract
            for contract in contracts
            if contract.conId > 0 and contract.conId not in exclude_con_ids
        ]
        if not contracts:
            raise RuntimeError("No unoccupied target put contract could be qualified")
        expiration_rank = {
            expiration: rank
            for rank, (expiration, _dte) in enumerate(eligible_expirations)
        }
        contracts.sort(
            key=lambda candidate: (
                expiration_rank.get(
                    candidate.lastTradeDateOrContractMonth,
                    len(expiration_rank),
                ),
                abs(float(candidate.strike) - strike_target),
            )
        )
        tickers = await self.ibkr.get_tickers_for_contracts(
            symbol,
            contracts,
            generic_tick_list="101",
            required_fields=[],
            optional_fields=[
                TickerField.MARKET_PRICE,
                TickerField.MIDPOINT,
                TickerField.OPEN_INTEREST,
            ],
        )
        rejected_quotes: list[tuple[PutQuote, Contract]] = []
        for ticker in tickers:
            try:
                quote = self._build_quote(underlying.price, ticker)
            except (RuntimeError, TypeError, ValueError):
                continue
            contract = ticker.contract
            if contract is None:
                continue
            if self._quote_rejection(target, quote) is None:
                return quote, contract
            rejected_quotes.append((quote, contract))
        if rejected_quotes:
            return rejected_quotes[0]
        raise RuntimeError("No target put contract has a usable quote")

    async def _underlying_quote(
        self,
        target: TailHedgeTargetConfig,
    ) -> UnderlyingQuote:
        symbol_config = self.config.portfolio.symbols[target.symbol]
        ticker = await self.ibkr.get_ticker_for_stock(
            target.symbol,
            symbol_config.primary_exchange or "",
            self.order_ops.get_order_exchange(),
        )
        if ticker.contract is None:
            raise RuntimeError("Underlying contract is unavailable")
        price = float(midpoint_or_market_price(ticker))
        if not self._is_positive(price):
            raise RuntimeError("Underlying market price is unavailable")
        return UnderlyingQuote(ticker.contract, price)

    async def _vix_price(self) -> float:
        if self._cached_vix is not None:
            return self._cached_vix
        ticker = await self.ibkr.get_ticker_for_contract(Index("VIX", "CBOE", "USD"))
        vix = float(ticker.marketPrice())
        if not self._is_positive(vix):
            raise RuntimeError("VIX market price is unavailable")
        self._cached_vix = vix
        return vix

    async def _option_ticker(self, contract: Contract) -> Ticker:
        return await self.ibkr.get_ticker_for_contract(
            contract,
            generic_tick_list="",
            required_fields=[],
            optional_fields=[TickerField.MARKET_PRICE, TickerField.MIDPOINT],
        )

    def _build_quote(self, underlying_price: float, ticker: Ticker) -> PutQuote:
        if ticker.contract is None:
            raise RuntimeError("Put ticker contract is unavailable")
        bid = float(ticker.bid)
        ask = float(ticker.ask)
        if not self._is_finite(bid) or bid < 0:
            raise RuntimeError("Put bid is unavailable")
        if not self._is_finite(ask) or ask < 0:
            raise RuntimeError("Put ask is unavailable")
        if ask < bid:
            raise RuntimeError("Put quote is crossed")
        midpoint = (bid + ask) / 2.0
        limit_price = round(midpoint, 2)
        if limit_price <= 0:
            raise RuntimeError("Put midpoint is below the minimum price tick")
        return PutQuote(
            expiration=ticker.contract.lastTradeDateOrContractMonth,
            dte=self._dte(ticker.contract.lastTradeDateOrContractMonth),
            underlying_price=underlying_price,
            con_id=ticker.contract.conId,
            local_symbol=ticker.contract.localSymbol,
            strike=float(ticker.contract.strike),
            bid=bid,
            ask=ask,
            open_interest=self._put_open_interest(ticker),
            midpoint=midpoint,
            limit_price=limit_price,
            premium_ratio=limit_price / underlying_price,
            bid_ask_ratio=self._bid_ask_ratio(bid, ask),
        )

    @staticmethod
    def _working_tail_orders(open_trades: List[Trade]) -> List[WorkingTailOrder]:
        working_orders: list[WorkingTailOrder] = []
        for trade in open_trades:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            order_ref = getattr(order, "orderRef", None)
            if order_ref not in TAIL_HEDGE_ORDER_REFS and not (
                isinstance(order_ref, str)
                and order_ref.startswith(TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX)
            ):
                continue
            status = getattr(trade, "orderStatus", None)
            working_orders.append(
                WorkingTailOrder(
                    broker_order=order,
                    order_ref=order_ref,
                    order_id=getattr(order, "orderId", None),
                    symbol=getattr(contract, "symbol", None),
                    con_id=getattr(contract, "conId", None),
                    status=getattr(status, "status", None),
                    filled=getattr(status, "filled", None),
                    remaining=getattr(status, "remaining", None),
                    limit_price=getattr(order, "lmtPrice", None),
                )
            )
        return working_orders

    def _account_open_trades(self) -> List[Trade]:
        account_number = self.config.runtime.account.number
        return [
            trade
            for trade in self.ibkr.open_trades()
            if getattr(getattr(trade, "order", None), "account", None) == account_number
        ]

    @staticmethod
    def _working_put_con_ids(open_trades: List[Trade]) -> set[int]:
        return {
            trade.contract.conId
            for trade in open_trades
            if isinstance(trade.contract, Option)
            and trade.contract.right.upper().startswith("P")
            and trade.contract.conId > 0
        }

    @staticmethod
    def _put_positions_by_con_id(
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Dict[int, PortfolioItem]:
        return {
            position.contract.conId: position
            for positions in portfolio_positions.values()
            for position in positions
            if isinstance(position.contract, Option)
            and position.contract.right.upper().startswith("P")
            and not math.isclose(float(position.position), 0.0)
        }

    def _queued_put_con_ids(self) -> set[int]:
        return {
            contract.conId
            for contract, _order, _intent_id in self.order_ops.orders.records()
            if isinstance(contract, Option)
            and contract.right.upper().startswith("P")
            and contract.conId > 0
        }

    def _same_run_regime_buy_symbols(self) -> set[str]:
        approved_symbols = (
            self._get_regime_buy_symbols()
            if self._get_regime_buy_symbols is not None
            else set()
        )
        queued_symbols = {
            contract.symbol
            for contract, order, _intent_id in self.order_ops.orders.records()
            if isinstance(contract, Stock)
            and str(getattr(order, "action", "")).upper() == "BUY"
            and str(getattr(order, "orderRef", "")).startswith("tg:regime-rebalance:")
        }
        return approved_symbols | queued_symbols

    @staticmethod
    def _stock_exposure(symbol_positions: List[PortfolioItem]) -> float:
        total_value = 0.0
        for position in symbol_positions:
            if not isinstance(position.contract, Stock) or position.position <= 0:
                continue
            market_value = float(getattr(position, "marketValue", 0.0) or 0.0)
            if not TailHedgeEngine._is_positive(market_value):
                market_price = float(getattr(position, "marketPrice", 0.0) or 0.0)
                if not TailHedgeEngine._is_positive(market_price):
                    continue
                market_value = float(position.position) * market_price
            total_value += market_value
        return total_value

    def _record_state(
        self,
        status: str,
        *,
        tranches: List[Dict[str, Any]],
        entry_history: List[Dict[str, Any]],
        harvest_plans: List[Dict[str, Any]],
        persistence_required: bool = True,
        **payload: Any,
    ) -> Dict[str, Any]:
        if self.data_store is None:
            raise RuntimeError("Tail hedge requires SQLite state storage.")
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
        recorded = self.data_store.record_event(TAIL_HEDGE_STATE_EVENT, state)
        if not recorded and persistence_required:
            raise RuntimeError("Failed to persist required tail-hedge state")
        if not recorded:
            log.warning(
                "Failed to persist tail-hedge "
                f"{status} state; continuing with risk-reducing management."
            )
        return state

    def _state_parts(
        self,
        state: Optional[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        if state is None:
            return [], [], []
        raw_tranches, raw_history, raw_harvest_plans = tail_hedge_state_collections(
            state,
            account_number=self.config.runtime.account.number,
        )

        tranches: list[dict[str, Any]] = []
        tranche_entry_ids: set[str] = set()
        tranche_con_ids: set[int] = set()
        tranche_symbols: dict[str, str] = {}
        for raw_tranche in raw_tranches:
            if not isinstance(raw_tranche, dict):
                raise RuntimeError("Tail-hedge state contains an invalid tranche")
            tranche = dict(raw_tranche)
            entry_id = tranche.get("entry_id")
            symbol = tranche.get("symbol")
            con_id = tranche.get("con_id")
            expiration = tranche.get("expiration")
            status = tranche.get("status")
            if (
                not isinstance(entry_id, str)
                or not entry_id
                or not isinstance(symbol, str)
                or not symbol
                or type(con_id) is not int
                or con_id <= 0
                or not isinstance(expiration, str)
                or not expiration
                or status not in {"entry_enqueued", "active", "close_enqueued"}
            ):
                raise RuntimeError("Tail-hedge state contains an invalid tranche")
            try:
                contract_date_to_datetime(expiration)
            except ValueError as exc:
                raise RuntimeError(
                    "Tail-hedge state contains an invalid expiration"
                ) from exc
            if entry_id in tranche_entry_ids or con_id in tranche_con_ids:
                raise RuntimeError("Tail-hedge state contains duplicate tranches")
            tranche_entry_ids.add(entry_id)
            tranche_con_ids.add(con_id)
            tranche_symbols[entry_id] = symbol
            tranches.append(tranche)

        entry_history: list[dict[str, Any]] = []
        history_entry_ids: set[str] = set()
        history_symbols: dict[str, str] = {}
        for raw_entry in raw_history:
            if not isinstance(raw_entry, dict):
                raise RuntimeError("Tail-hedge state contains invalid entry history")
            entry = dict(raw_entry)
            entry_id = entry.get("entry_id")
            symbol = entry.get("symbol")
            entered_at = self._parse_datetime(entry.get("entered_at"))
            raw_estimated_cost = entry.get("estimated_cost")
            if isinstance(raw_estimated_cost, bool) or not isinstance(
                raw_estimated_cost,
                (int, float, str),
            ):
                raise RuntimeError("Tail-hedge state contains invalid entry history")
            try:
                estimated_cost = float(raw_estimated_cost)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Tail-hedge state contains invalid entry history"
                ) from exc
            if (
                not isinstance(entry_id, str)
                or not entry_id
                or not isinstance(symbol, str)
                or not symbol
                or entry_id in history_entry_ids
                or entered_at is None
                or not self._is_finite(estimated_cost)
                or estimated_cost < 0
            ):
                raise RuntimeError("Tail-hedge state contains invalid entry history")
            history_entry_ids.add(entry_id)
            history_symbols[entry_id] = symbol
            entry_history.append(entry)
        if not tranche_entry_ids.issubset(history_entry_ids):
            raise RuntimeError("Tail-hedge state is missing tranche entry history")
        if any(
            tranche_symbols[entry_id] != history_symbols[entry_id]
            for entry_id in tranche_entry_ids
        ):
            raise RuntimeError("Tail-hedge state has mismatched tranche ownership")
        harvest_plans: list[dict[str, Any]] = []
        plan_ids: set[str] = set()
        active_symbols: set[str] = set()
        valid_statuses = TAIL_HEDGE_ACTIVE_HARVEST_STATUSES | {"completed", "canceled"}
        for raw_plan in raw_harvest_plans:
            plan = dict(raw_plan)
            plan_id = plan.get("plan_id")
            symbol = plan.get("symbol")
            status = plan.get("status")
            put_sales = plan.get("put_sales")
            if (
                not isinstance(plan_id, str)
                or not plan_id
                or plan_id in plan_ids
                or not isinstance(symbol, str)
                or not symbol
                or status not in valid_statuses
                or not isinstance(put_sales, list)
                or not all(isinstance(sale, dict) for sale in put_sales)
            ):
                raise RuntimeError("Tail-hedge state contains an invalid harvest plan")
            if status in TAIL_HEDGE_ACTIVE_HARVEST_STATUSES:
                if symbol in active_symbols:
                    raise RuntimeError(
                        "Tail-hedge state contains duplicate active harvest plans"
                    )
                active_symbols.add(symbol)
            plan_ids.add(plan_id)
            harvest_plans.append(plan)

        return tranches, entry_history, harvest_plans

    def _record_evaluation(
        self,
        outcome: str,
        *,
        symbol: Optional[str] = None,
        **payload: Any,
    ) -> None:
        if self.data_store is None:
            return
        self.data_store.record_event(
            TAIL_HEDGE_EVALUATION_EVENT,
            {
                "schema_version": TAIL_HEDGE_STATE_SCHEMA_VERSION,
                "account": self.config.runtime.account.number,
                "evaluated_at": self._now(),
                "symbol": symbol,
                "outcome": outcome,
                **payload,
            },
            symbol=symbol,
        )

    def _dte(self, expiration: str) -> int:
        return (contract_date_to_datetime(expiration).date() - self._now().date()).days

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value).replace(tzinfo=None)
        except ValueError:
            return None

    @staticmethod
    def _midpoint(ticker: Ticker) -> float:
        midpoint = float(ticker.midpoint())
        if not TailHedgeEngine._is_finite(midpoint):
            midpoint = float(midpoint_or_market_price(ticker))
        if not TailHedgeEngine._is_finite(midpoint) or midpoint < 0:
            raise RuntimeError("Option midpoint is unavailable")
        return midpoint

    @staticmethod
    def _put_open_interest(ticker: Ticker) -> float:
        value = float(ticker.putOpenInterest)
        return value if TailHedgeEngine._is_finite(value) else 0.0

    @staticmethod
    def _bid_ask_ratio(bid: float, ask: float) -> float:
        midpoint = (bid + ask) / 2.0
        if midpoint <= 0:
            return math.inf
        return (ask - bid) / midpoint

    @staticmethod
    def _multiplier(contract: Contract) -> float:
        multiplier = float(contract.multiplier or 100)
        if not TailHedgeEngine._is_positive(multiplier):
            raise RuntimeError("Put contract multiplier is unavailable")
        return multiplier

    @staticmethod
    def _position_quantity(position: PortfolioItem) -> int:
        quantity = abs(float(position.position))
        rounded = round(quantity)
        if quantity <= 0 or not math.isclose(quantity, rounded):
            raise RuntimeError(
                "Tail-hedge option position must have a positive whole-contract "
                f"quantity, got {position.position}"
            )
        return int(rounded)

    @staticmethod
    def _is_finite(value: float) -> bool:
        return math.isfinite(value) and not util.isNan(value)

    @staticmethod
    def _is_positive(value: float) -> bool:
        return TailHedgeEngine._is_finite(value) and value > 0
