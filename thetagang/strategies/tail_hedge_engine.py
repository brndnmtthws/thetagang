from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from ib_async import PortfolioItem, Ticker, Trade, util
from ib_async.contract import Contract, Index, Option, Stock
from ib_async.wrapper import RequestError

from thetagang import log
from thetagang.config import Config
from thetagang.config_models import TailHedgeTargetConfig
from thetagang.db import DataStore
from thetagang.fmt import dfmt
from thetagang.ibkr import IBKR, RequiredFieldValidationError, TickerField
from thetagang.options import contract_date_to_datetime
from thetagang.strategies.tail_hedge_state import (
    TAIL_HEDGE_CLOSE_ORDER_REF,
    TAIL_HEDGE_ENTRY_ORDER_REF,
    TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX,
    TailHedgeCohort,
    TailHedgeState,
    TailHedgeStateStore,
    build_tail_reduction_order_ref,
    is_tail_order_ref,
    is_tail_reduction_ref,
    parse_state_datetime,
)
from thetagang.trading_operations import OrderOperations
from thetagang.util import midpoint_or_market_price, working_stock_order_symbols

TAIL_HEDGE_EVALUATION_EVENT = "tail_hedge_evaluation"
TAIL_HEDGE_EVALUATION_SCHEMA_VERSION = 2
TAIL_ORDER_RECONCILIATION_GRACE = timedelta(minutes=5)
TAIL_HEDGE_ERRORS = (
    IndexError,
    RequestError,
    RequiredFieldValidationError,
    RuntimeError,
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
    catastrophe_drawdowns: tuple[float, ...] = ()
    catastrophe_payouts: tuple[float, ...] = ()
    catastrophe_payout_multiple: float = 0.0
    estimated_fee_per_contract: float = 0.0
    all_in_cost_per_contract: float = 0.0


@dataclass(frozen=True)
class UnderlyingQuote:
    contract: Contract
    price: float


@dataclass(frozen=True)
class BrokerOrderProgress:
    status: str
    filled: float
    observed_at: datetime | None
    intent_specific: bool = False

    @property
    def is_filled(self) -> bool:
        return self.status.lower() == "filled"

    @property
    def is_terminal(self) -> bool:
        return self.status.lower() in {
            "apicancelled",
            "apicanceled",
            "cancelled",
            "canceled",
            "filled",
            "inactive",
        }


class NoEligibleExpirationError(RuntimeError):
    """Raised when the chain has no expiration inside the configured DTE window."""


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
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.order_ops = order_ops
        self.data_store = data_store
        self._now = now_provider
        self.state_store = (
            TailHedgeStateStore(
                data_store,
                config.runtime.account.number,
            )
            if data_store is not None
            else None
        )
        self._cached_vix: Optional[float] = None
        self._run_outcomes: dict[str, str] = {}

    def _estimated_fee_per_contract(self) -> float:
        orders = getattr(self.config.runtime, "orders", None)
        return float(
            getattr(
                orders,
                "estimated_fee_per_contract",
                0.0,
            )
        )

    def _all_in_contract_cost(self, limit_price: float, multiplier: float) -> float:
        return round(
            limit_price * multiplier + self._estimated_fee_per_contract(),
            2,
        )

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
        self._require_state_store()

        log.notice("Evaluating tail-hedge long-put program...")
        self._cached_vix = None
        self._run_outcomes = {}
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
        state_store = self._require_state_store()
        state = state_store.load()
        open_trades = self._account_open_trades()
        account_trades = self._account_trades()
        tail_trades = [
            trade
            for trade in open_trades
            if is_tail_order_ref(getattr(trade.order, "orderRef", None))
        ]
        entry_trades = [
            trade
            for trade in tail_trades
            if trade.order.orderRef == TAIL_HEDGE_ENTRY_ORDER_REF
        ]
        close_trades = [
            trade
            for trade in tail_trades
            if trade.order.orderRef != TAIL_HEDGE_ENTRY_ORDER_REF
        ]
        pending_close_con_ids = self._queued_tail_close_con_ids() | {
            trade.contract.conId for trade in close_trades if trade.contract.conId > 0
        }
        pending_close_con_ids |= self._reconcile_state(
            state=state,
            entry_trades=entry_trades,
            account_trades=account_trades,
            pending_close_con_ids=pending_close_con_ids,
        )
        targets = {
            target.symbol: target
            for target in self.config.strategies.tail_hedge.targets
        }

        blocked_entry_symbols = (
            {trade.contract.symbol for trade in close_trades}
            | working_stock_order_symbols(
                open_trades,
                self.config.runtime.account.number,
            )
            | self._same_run_stock_trade_symbols()
            | {
                cohort.symbol
                for cohort in state.open_cohorts
                if cohort.has_pending_recovery
            }
        )
        entry_blockers: dict[str, set[str]] = {}
        for trade in close_trades:
            entry_blockers.setdefault(trade.contract.symbol, set()).add(
                "working_tail_close"
            )
        for symbol in working_stock_order_symbols(
            open_trades,
            self.config.runtime.account.number,
        ):
            entry_blockers.setdefault(symbol, set()).add("working_stock_order")
        for symbol in self._same_run_stock_trade_symbols():
            entry_blockers.setdefault(symbol, set()).add("same_run_stock_order")
        for cohort in state.open_cohorts:
            if cohort.has_pending_recovery:
                entry_blockers.setdefault(cohort.symbol, set()).add("pending_recovery")
        for cohort in state.open_cohorts:
            con_id = cohort.con_id
            symbol = cohort.symbol
            position = self._account_put_positions_by_con_id().get(con_id)
            if position is None:
                continue
            if con_id in pending_close_con_ids or not self.config.trading_is_allowed(
                symbol
            ):
                blocked_entry_symbols.add(symbol)
                blocker = (
                    "working_tail_close"
                    if con_id in pending_close_con_ids
                    else "trading_disabled"
                )
                entry_blockers.setdefault(symbol, set()).add(blocker)
                continue
            try:
                close_enqueued = await self._manage_existing_put(
                    position,
                    targets.get(symbol),
                    cohort,
                    state,
                )
            except TAIL_HEDGE_ERRORS as exc:
                blocked_entry_symbols.add(symbol)
                self._record_error(symbol, exc)
            else:
                if close_enqueued:
                    blocked_entry_symbols.add(symbol)
                    entry_blockers.setdefault(symbol, set()).add("close_enqueued")

        occupied_con_ids = set(self._account_put_positions_by_con_id())
        occupied_con_ids |= self._queued_put_con_ids()
        occupied_con_ids |= self._working_put_con_ids(open_trades)
        working_entry_symbols = {trade.contract.symbol for trade in entry_trades} | {
            cohort.symbol
            for cohort in state.open_cohorts
            if cohort.status == "entry_enqueued"
        }
        for target in self.config.strategies.tail_hedge.targets:
            symbol = target.symbol
            if symbol in blocked_entry_symbols or symbol in working_entry_symbols:
                if symbol in working_entry_symbols:
                    self._record_evaluation("working_order_present", symbol=symbol)
                else:
                    blockers = ",".join(
                        sorted(entry_blockers.get(symbol, {"tail_action_in_progress"}))
                    )
                    self._run_outcomes[symbol] = f"entry_blocked:{blockers}"
                continue
            try:
                await self._evaluate_entry(
                    target,
                    self._stock_exposure(portfolio_positions.get(symbol, [])),
                    net_liquidation=net_liquidation,
                    state=state,
                    occupied_con_ids=occupied_con_ids,
                )
            except TAIL_HEDGE_ERRORS as exc:
                self._record_error(symbol, exc)
        self._log_program_summary(state, net_liquidation=net_liquidation)

    def _reconcile_state(
        self,
        *,
        state: TailHedgeState,
        entry_trades: List[Trade],
        account_trades: List[Trade],
        pending_close_con_ids: set[int],
    ) -> set[int]:
        put_positions = self._account_put_positions_by_con_id()
        changed = False
        for cohort in list(state.open_cohorts):
            position = put_positions.get(cohort.con_id)
            if position is None:
                changed |= self._reconcile_missing_position(
                    state=state,
                    cohort=cohort,
                    entry_trades=entry_trades,
                    account_trades=account_trades,
                    pending_close_con_ids=pending_close_con_ids,
                )
            else:
                changed |= self._reconcile_live_position(
                    cohort=cohort,
                    position=position,
                    entry_trades=entry_trades,
                    account_trades=account_trades,
                    pending_close_con_ids=pending_close_con_ids,
                )
        if changed:
            self._require_state_store().save(state)
        return {
            cohort.con_id
            for cohort in state.open_cohorts
            if cohort.has_pending_recovery
        }

    def _reconcile_missing_position(
        self,
        *,
        state: TailHedgeState,
        cohort: TailHedgeCohort,
        entry_trades: List[Trade],
        account_trades: List[Trade],
        pending_close_con_ids: set[int],
    ) -> bool:
        if cohort.status == "entry_enqueued":
            entry_working = self._entry_is_working(cohort, entry_trades)
            progress = self._latest_tail_order_progress(
                account_trades,
                con_id=cohort.con_id,
                symbol=cohort.symbol,
                action="BUY",
                enqueued_at=cohort.entered_at,
            )
            confirmed_fill = progress is not None and (
                progress.filled > 0 or progress.is_filled
            )
            confirmed_zero_fill_cancel = (
                progress is not None and progress.is_terminal and not confirmed_fill
            )
            if (
                entry_working
                or (progress is not None and not progress.is_terminal)
                or (
                    self._within_reconciliation_grace(cohort.entered_at)
                    and not confirmed_zero_fill_cancel
                )
            ):
                return False
            if confirmed_fill:
                cohort.close()
                log.notice(
                    f"{cohort.symbol}: Reconciled tail entry {cohort.con_id} as "
                    "closed because no live position remains."
                )
            else:
                state.cohorts.remove(cohort)
                log.info(
                    f"{cohort.symbol}: Tail entry {cohort.con_id} ended without a "
                    "fill; released its cadence and budget reservation."
                )
            return True

        progress = self._reduction_progress(cohort, account_trades)
        if cohort.has_pending_recovery and (
            cohort.con_id in pending_close_con_ids
            or (progress is not None and not progress.is_terminal)
            or (
                progress is None
                and self._within_reconciliation_grace(
                    cohort.pending_recovery_enqueued_at
                )
            )
        ):
            return False
        if (
            cohort.has_pending_recovery
            and progress is not None
            and (progress.observed_at is not None or progress.intent_specific)
        ):
            confirmed_quantity = min(
                cohort.pending_recovery_quantity or 0,
                max(
                    0,
                    math.floor(progress.filled) - cohort.accounted_recovery_quantity,
                ),
            )
            if progress.is_filled and math.floor(progress.filled) == 0:
                confirmed_quantity = cohort.pending_recovery_quantity or 0
            credited = cohort.apply_recovery(confirmed_quantity)
            if credited > 0:
                log.notice(
                    f"{cohort.symbol}: Credited {dfmt(credited)} of recovered tail "
                    f"premium for conId {cohort.con_id}."
                )
        cohort.close()
        log.notice(
            f"{cohort.symbol}: Closed tail cohort for conId {cohort.con_id}; "
            "the position is no longer present."
        )
        return True

    def _reconcile_live_position(
        self,
        *,
        cohort: TailHedgeCohort,
        position: PortfolioItem,
        entry_trades: List[Trade],
        account_trades: List[Trade],
        pending_close_con_ids: set[int],
    ) -> bool:
        entry_working = cohort.status == "entry_enqueued" and self._entry_is_working(
            cohort, entry_trades
        )
        live_quantity = self._position_quantity(position)
        if cohort.status == "entry_enqueued":
            progress = self._latest_tail_order_progress(
                account_trades,
                con_id=cohort.con_id,
                symbol=cohort.symbol,
                action="BUY",
                enqueued_at=cohort.entered_at,
            )
            confirmed_fill_quantity = (
                math.floor(progress.filled) if progress is not None else 0
            )
            if (
                entry_working
                or (progress is not None and not progress.is_terminal)
                or (
                    progress is None
                    and self._within_reconciliation_grace(cohort.entered_at)
                )
                or confirmed_fill_quantity > live_quantity
            ):
                return False
            cohort.status = "active"
            cohort.quantity = live_quantity
            settled_cost = round(
                live_quantity
                * self._all_in_contract_cost(
                    cohort.entry_limit_price,
                    self._multiplier(position.contract),
                ),
                2,
            )
            cohort.estimated_cost = min(cohort.estimated_cost, settled_cost)
            log.notice(
                f"{cohort.symbol}: Tail entry is active for conId {cohort.con_id}: "
                f"{live_quantity} contract(s), estimated cost "
                f"{dfmt(cohort.estimated_cost)}."
            )
            return True

        changed = live_quantity != cohort.quantity
        recovery_credited = 0.0
        if live_quantity > cohort.quantity:
            settled_cost = round(
                live_quantity
                * self._all_in_contract_cost(
                    cohort.entry_limit_price,
                    self._multiplier(position.contract),
                ),
                2,
            )
            cohort.estimated_cost = max(cohort.estimated_cost, settled_cost)
        if live_quantity < cohort.quantity and cohort.has_pending_recovery:
            recovery_credited = cohort.apply_recovery(cohort.quantity - live_quantity)
            if recovery_credited > 0:
                log.notice(
                    f"{cohort.symbol}: Credited {dfmt(recovery_credited)} of "
                    "recovered tail "
                    f"premium for conId {cohort.con_id}."
                )
        cohort.quantity = live_quantity
        if not cohort.has_pending_recovery:
            return changed

        progress = self._reduction_progress(cohort, account_trades)
        broker_fill_not_observed = progress is not None and (
            progress.filled > cohort.accounted_recovery_quantity
            or (progress.is_filled and (cohort.pending_recovery_quantity or 0) > 0)
        )
        keep_recovery = (cohort.pending_recovery_quantity or 0) > 0 and (
            cohort.con_id in pending_close_con_ids
            or (
                progress is None
                and self._within_reconciliation_grace(
                    cohort.pending_recovery_enqueued_at
                )
            )
            or (
                progress is not None
                and (not progress.is_terminal or broker_fill_not_observed)
            )
        )
        if keep_recovery:
            return changed
        cohort.clear_recovery()
        if recovery_credited <= 0:
            log.info(
                f"{cohort.symbol}: Tail reduction for conId {cohort.con_id} ended "
                "without an observed position change; it can be retried."
            )
        return True

    @staticmethod
    def _entry_is_working(cohort: TailHedgeCohort, trades: List[Trade]) -> bool:
        return any(
            trade.contract.symbol == cohort.symbol
            and (trade.contract.conId == cohort.con_id or trade.contract.conId <= 0)
            for trade in trades
        )

    def _reduction_progress(
        self,
        cohort: TailHedgeCohort,
        trades: List[Trade],
    ) -> BrokerOrderProgress | None:
        if not cohort.has_pending_recovery:
            return None
        return self._latest_tail_order_progress(
            trades,
            con_id=cohort.con_id,
            symbol=cohort.symbol,
            action="SELL",
            enqueued_at=cohort.pending_recovery_enqueued_at,
        )

    def _record_error(self, symbol: str, exc: Exception) -> None:
        self._record_evaluation(
            "evaluation_error",
            symbol=symbol,
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        log.error(f"{symbol}: Tail-hedge evaluation failed: {exc}")

    async def _manage_existing_put(
        self,
        position: PortfolioItem,
        target: Optional[TailHedgeTargetConfig],
        cohort: TailHedgeCohort,
        state: TailHedgeState,
    ) -> bool:
        symbol = cohort.symbol
        if position.contract.symbol != symbol:
            raise RuntimeError(
                f"Owned contract symbol {position.contract.symbol} does not match "
                f"state symbol {symbol}"
            )

        if float(position.position) < 0:
            action, reason = "BUY", "owned_put_is_short"
        elif target is None:
            action, reason = "SELL", "target_removed"
        elif (
            self._dte(position.contract.lastTradeDateOrContractMonth) <= target.exit_dte
        ):
            action, reason = "SELL", "roll_dte"
        else:
            self._record_evaluation(
                "long_put_held",
                symbol=symbol,
                entry_id=cohort.entry_id,
                con_id=position.contract.conId,
            )
            return False

        await self._close_position(
            position,
            cohort,
            state,
            action=action,
            close_reason=reason,
        )
        return True

    async def _close_position(
        self,
        position: PortfolioItem,
        cohort: TailHedgeCohort,
        state: TailHedgeState,
        *,
        action: str,
        close_reason: str,
    ) -> None:
        symbol = cohort.symbol
        position.contract.exchange = self.order_ops.get_order_exchange()
        ticker = await self._option_ticker(position.contract)
        limit_price = round(max(self._midpoint(ticker), 0.01), 2)
        # Market-data retrieval yields to the IB event loop. A working close can
        # fill during that await, replacing or removing the PortfolioItem that
        # was supplied above. Prove the current direction and size from the live
        # account cache immediately before enqueueing another closing order.
        live_position = self._account_put_positions_by_con_id().get(
            position.contract.conId
        )
        expected_position_sign = 1 if action == "SELL" else -1
        if live_position is None or (
            float(live_position.position) * expected_position_sign <= 0
        ):
            self._record_evaluation(
                "position_changed_before_close",
                symbol=symbol,
                entry_id=cohort.entry_id,
                con_id=position.contract.conId,
                expected_action=action,
                close_reason=close_reason,
            )
            return
        position = live_position
        quantity = self._position_quantity(position)
        position.contract.exchange = self.order_ops.get_order_exchange()
        estimated_fee = self._estimated_fee_per_contract()
        estimated_net_proceeds = max(
            0.0,
            round(
                limit_price * self._multiplier(position.contract) - estimated_fee,
                2,
            ),
        )
        if action == "SELL":
            # A reduction observed during the quote await predates this order.
            # Sync ownership without treating it as recovered premium.
            cohort.quantity = min(cohort.quantity, quantity)
            quantity = cohort.quantity
            cohort.begin_recovery(
                quantity=quantity,
                proceeds_per_contract=estimated_net_proceeds,
                enqueued_at=self._now(),
            )
            self._require_state_store().save(state)
        order = self.order_ops.create_limit_order(
            action=action,
            quantity=quantity,
            limit_price=limit_price,
            order_ref=(
                build_tail_reduction_order_ref(
                    TAIL_HEDGE_CLOSE_ORDER_REF,
                    cohort.con_id,
                    cohort.pending_recovery_enqueued_at,
                )
                if action == "SELL"
                else TAIL_HEDGE_CLOSE_ORDER_REF
            ),
            transmit=True,
        )
        self.order_ops.enqueue_order(position.contract, order)

        self._record_evaluation(
            "close_enqueued",
            symbol=symbol,
            entry_id=cohort.entry_id,
            con_id=position.contract.conId,
            quantity=quantity,
            action=action,
            limit_price=limit_price,
            estimated_fee_per_contract=estimated_fee,
            estimated_net_proceeds_per_contract=estimated_net_proceeds,
            close_reason=close_reason,
        )
        log.notice(
            f"{symbol}: Enqueued tail close for {quantity} contract(s), "
            f"conId {position.contract.conId}, at {dfmt(limit_price)}; "
            f"estimated net proceeds={dfmt(estimated_net_proceeds)}/contract "
            f"after {dfmt(estimated_fee)} fee; reason={close_reason}."
        )

    async def _evaluate_entry(
        self,
        target: TailHedgeTargetConfig,
        stock_exposure: float,
        *,
        net_liquidation: float,
        state: TailHedgeState,
        occupied_con_ids: set[int],
    ) -> None:
        state_store = self._require_state_store()
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
        recent_cohorts = state.recent_cohorts(now)
        target_history = [
            cohort for cohort in recent_cohorts if cohort.symbol == symbol
        ]
        entered_at = [cohort.entered_at for cohort in target_history]
        if (
            entered_at
            and (now - max(entered_at)).days < target.minimum_entry_spacing_days
        ):
            self._record_evaluation("minimum_entry_spacing", symbol=symbol)
            return

        budget_cohorts = [
            cohort
            for cohort in state.cohorts
            if cohort in recent_cohorts or cohort.is_open
        ]
        global_spent = sum(cohort.net_charge for cohort in budget_cohorts)
        target_spent = sum(
            cohort.net_charge for cohort in budget_cohorts if cohort.symbol == symbol
        )

        def entry_budget(current_nlv: float) -> float:
            budget = current_nlv * float(
                self.config.strategies.tail_hedge.annual_budget
            )
            target_budget = budget * target.budget_weight
            return max(
                0.0,
                min(
                    budget - global_spent,
                    target_budget - target_spent,
                    target_budget / target.entries_per_year,
                ),
            )

        applicable_budget = entry_budget(net_liquidation)
        if applicable_budget <= 0:
            self._record_evaluation("annual_budget_exhausted", symbol=symbol)
            return

        vix: Optional[float] = None
        if target.entry_gate == "vix":
            vix = await self._vix_price()
            if vix > target.entry_vix_max:
                self._record_evaluation("vix_above_entry_max", symbol=symbol, vix=vix)
                return

        try:
            quote, contract = await self._find_put(
                target,
                exclude_con_ids=occupied_con_ids,
            )
        except NoEligibleExpirationError:
            self._record_evaluation(
                "no_eligible_expiration_available",
                symbol=symbol,
            )
            return

        rejection = self._quote_rejection(target, quote)
        if rejection is not None:
            self._record_evaluation(rejection, symbol=symbol, quote=asdict(quote))
            return

        applicable_budget = entry_budget(
            self.ibkr.cached_net_liquidation(self.config.runtime.account.number)
        )
        if applicable_budget <= 0:
            self._record_evaluation("annual_budget_exhausted", symbol=symbol)
            return

        premium_per_contract = round(
            quote.limit_price * self._multiplier(contract),
            2,
        )
        per_contract_cost = self._all_in_contract_cost(
            quote.limit_price,
            self._multiplier(contract),
        )
        quantity = math.floor(applicable_budget / per_contract_cost)
        if quantity < 1:
            self._record_evaluation(
                "contract_exceeds_applicable_budget",
                symbol=symbol,
                applicable_budget=applicable_budget,
                premium_per_contract=premium_per_contract,
                estimated_fee_per_contract=self._estimated_fee_per_contract(),
                per_contract_cost=per_contract_cost,
                quote=asdict(quote),
            )
            return
        account_number = self.config.runtime.account.number
        live_positions = self.ibkr.portfolio(account=account_number)
        live_open_trades = self._account_open_trades()
        live_stock_exposure = self._stock_exposure(
            [
                position
                for position in live_positions
                if position.contract.symbol == symbol
            ]
        )
        if (
            live_stock_exposure <= 0
            or symbol in self._same_run_stock_trade_symbols()
            or symbol
            in working_stock_order_symbols(
                live_open_trades,
                account_number,
            )
        ):
            self._record_evaluation("protected_position_changed", symbol=symbol)
            return
        live_put_con_ids = {
            position.contract.conId
            for position in live_positions
            if isinstance(position.contract, Option)
            and position.contract.right.upper().startswith("P")
            and position.contract.conId > 0
            and not math.isclose(float(position.position), 0.0)
        }
        currently_occupied_con_ids = live_put_con_ids
        currently_occupied_con_ids |= self._working_put_con_ids(live_open_trades)
        currently_occupied_con_ids |= self._queued_put_con_ids()
        if quote.con_id in currently_occupied_con_ids:
            self._record_evaluation(
                "target_put_became_occupied",
                symbol=symbol,
                con_id=quote.con_id,
            )
            return
        entry_cost = round(per_contract_cost * quantity, 2)
        estimated_fees = round(self._estimated_fee_per_contract() * quantity, 2)
        quantity_to_open_interest = (
            quantity / quote.open_interest if quote.open_interest > 0 else None
        )
        if quantity_to_open_interest is not None and quantity_to_open_interest > 1.0:
            log.warning(
                f"{symbol}: Tail entry quantity {quantity} exceeds quoted open "
                f"interest {quote.open_interest:g}; "
                f"ratio={quantity_to_open_interest:.2f}."
            )
        entered_at = self._now()
        entry_id = f"{symbol}:{quote.con_id}:{entered_at.isoformat()}"
        state.prune_closed(now)
        state.cohorts.append(
            TailHedgeCohort(
                entry_id=entry_id,
                symbol=symbol,
                status="entry_enqueued",
                con_id=quote.con_id,
                expiration=quote.expiration,
                strike=quote.strike,
                quantity=quantity,
                entry_limit_price=quote.limit_price,
                entered_at=entered_at,
                estimated_cost=entry_cost,
            )
        )
        state_store.save(state)
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
            entry_cost=entry_cost,
            premium_cost=round(premium_per_contract * quantity, 2),
            estimated_fees=estimated_fees,
            applicable_budget=applicable_budget,
            quantity_to_open_interest=quantity_to_open_interest,
            order_exceeds_open_interest=(
                quantity_to_open_interest is not None
                and quantity_to_open_interest > 1.0
            ),
            quote=asdict(quote),
        )
        log.notice(
            f"{symbol}: Enqueued {quantity}x {quote.strike:g} puts expiring "
            f"{quote.expiration} at {dfmt(quote.limit_price)} each; "
            f"estimated fees={dfmt(estimated_fees)}, "
            f"catastrophe payout multiple="
            f"{quote.catastrophe_payout_multiple:.2f}x."
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
        expiration_dtes = [
            (expiration, self._dte(expiration)) for expiration in chain.expirations
        ]
        eligible_expirations = [
            (expiration, dte)
            for expiration, dte in expiration_dtes
            if target.min_dte <= dte <= target.max_dte
        ]
        if not eligible_expirations:
            raise NoEligibleExpirationError(
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
                    multiplier=chain.multiplier,
                    currency="USD",
                    tradingClass=chain.tradingClass,
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
        tickers_by_con_id = {
            ticker.contract.conId: ticker
            for ticker in tickers
            if ticker.contract is not None and ticker.contract.conId > 0
        }
        eligible_quotes: list[tuple[PutQuote, Contract]] = []
        rejected_quotes: list[tuple[PutQuote, Contract]] = []
        for contract in contracts:
            ticker = tickers_by_con_id.get(contract.conId)
            if ticker is None:
                continue
            try:
                quote = self._build_quote(underlying.price, ticker)
            except (RuntimeError, TypeError, ValueError):
                continue
            quote = self._with_catastrophe_metrics(target, quote, contract)
            if self._quote_rejection(target, quote) is None:
                eligible_quotes.append((quote, contract))
            else:
                rejected_quotes.append((quote, contract))
        if eligible_quotes:
            eligible_quotes.sort(
                key=lambda item: (
                    expiration_rank.get(
                        item[1].lastTradeDateOrContractMonth,
                        len(expiration_rank),
                    ),
                    -item[0].catastrophe_payout_multiple,
                    item[0].bid_ask_ratio,
                    -item[0].open_interest,
                    abs(item[0].strike - strike_target),
                    item[0].con_id,
                )
            )
            quote, contract = eligible_quotes[0]
            log.info(
                f"{symbol}: Selected tail put conId={quote.con_id} "
                f"expiry={quote.expiration} strike={quote.strike:g} "
                f"catastrophe_multiple="
                f"{quote.catastrophe_payout_multiple:.2f}x "
                f"drawdowns={quote.catastrophe_drawdowns} "
                f"payouts={quote.catastrophe_payouts}."
            )
            return quote, contract
        if rejected_quotes:
            return rejected_quotes[0]
        raise RuntimeError("No target put contract has a usable quote")

    def _with_catastrophe_metrics(
        self,
        target: TailHedgeTargetConfig,
        quote: PutQuote,
        contract: Contract,
    ) -> PutQuote:
        multiplier = self._multiplier(contract)
        drawdowns = tuple(float(value) for value in target.catastrophe_drawdowns)
        payouts = tuple(
            round(
                max(
                    quote.strike - quote.underlying_price * (1.0 - drawdown),
                    0.0,
                )
                * multiplier,
                2,
            )
            for drawdown in drawdowns
        )
        all_in_cost = self._all_in_contract_cost(quote.limit_price, multiplier)
        payout_multiple = (
            sum(payouts) / len(payouts) / all_in_cost if all_in_cost > 0 else 0.0
        )
        return replace(
            quote,
            catastrophe_drawdowns=drawdowns,
            catastrophe_payouts=payouts,
            catastrophe_payout_multiple=round(payout_multiple, 6),
            estimated_fee_per_contract=self._estimated_fee_per_contract(),
            all_in_cost_per_contract=all_in_cost,
        )

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

    def _account_open_trades(self) -> List[Trade]:
        account_number = self.config.runtime.account.number
        return [
            trade
            for trade in self.ibkr.open_trades()
            if getattr(getattr(trade, "order", None), "account", None) == account_number
        ]

    def _account_trades(self) -> List[Trade]:
        account_number = self.config.runtime.account.number
        return [
            trade
            for trade in self.ibkr.trades()
            if getattr(getattr(trade, "order", None), "account", None) == account_number
        ]

    def _latest_tail_order_progress(
        self,
        trades: List[Trade],
        *,
        con_id: int,
        symbol: str,
        action: str,
        enqueued_at: datetime | None,
    ) -> BrokerOrderProgress | None:
        candidates: list[tuple[int, int, Trade, bool]] = []
        for index, trade in enumerate(trades):
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            order_ref = getattr(order, "orderRef", None)
            if (
                contract is None
                or getattr(contract, "symbol", None) != symbol
                or getattr(contract, "conId", 0) != con_id
                or str(getattr(order, "action", "")).upper() != action
                or (action == "BUY" and order_ref != TAIL_HEDGE_ENTRY_ORDER_REF)
                or (action == "SELL" and not is_tail_reduction_ref(order_ref))
            ):
                continue
            trade_time = self._trade_time(trade)
            intent_specific = False
            if enqueued_at is not None:
                if action == "SELL":
                    expected_refs = {
                        build_tail_reduction_order_ref(
                            TAIL_HEDGE_CLOSE_ORDER_REF,
                            con_id,
                            enqueued_at,
                        ),
                        build_tail_reduction_order_ref(
                            f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:{symbol}",
                            con_id,
                            enqueued_at,
                        ),
                    }
                    legacy_refs = {
                        TAIL_HEDGE_CLOSE_ORDER_REF,
                        f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:{symbol}:{con_id}",
                    }
                    intent_specific = order_ref in expected_refs
                    if not intent_specific and (
                        order_ref not in legacy_refs
                        or trade_time is None
                        or trade_time < enqueued_at
                    ):
                        continue
                elif trade_time is not None and trade_time < enqueued_at:
                    continue
            order_id = int(getattr(order, "orderId", 0) or 0)
            candidates.append((order_id, index, trade, intent_specific))
        if not candidates:
            return None

        selected = max(candidates, key=lambda candidate: candidate[:2])
        trade = selected[2]
        order_status = getattr(trade, "orderStatus", None)
        status = str(getattr(order_status, "status", "") or "")
        try:
            filled = float(getattr(order_status, "filled", 0.0) or 0.0)
        except (TypeError, ValueError):
            filled = 0.0
        if not math.isfinite(filled) or filled < 0:
            filled = 0.0
        return BrokerOrderProgress(
            status=status,
            filled=filled,
            observed_at=self._trade_time(trade),
            intent_specific=selected[3],
        )

    @staticmethod
    def _trade_time(trade: Trade) -> datetime | None:
        timestamps: list[datetime] = []
        for log_entry in getattr(trade, "log", ()) or ():
            timestamp = parse_state_datetime(getattr(log_entry, "time", None))
            if timestamp is not None:
                timestamps.append(timestamp)
        for fill in getattr(trade, "fills", ()) or ():
            execution = getattr(fill, "execution", None)
            timestamp = parse_state_datetime(getattr(execution, "time", None))
            if timestamp is not None:
                timestamps.append(timestamp)
        return max(timestamps) if timestamps else None

    def _within_reconciliation_grace(self, enqueued_at: datetime | None) -> bool:
        return enqueued_at is not None and (
            self._now() - enqueued_at < TAIL_ORDER_RECONCILIATION_GRACE
        )

    def _account_put_positions_by_con_id(self) -> Dict[int, PortfolioItem]:
        account_number = self.config.runtime.account.number
        return {
            position.contract.conId: position
            for position in self.ibkr.portfolio(account=account_number)
            if isinstance(position.contract, Option)
            and position.contract.right.upper().startswith("P")
            and not math.isclose(float(position.position), 0.0)
        }

    @staticmethod
    def _working_put_con_ids(open_trades: List[Trade]) -> set[int]:
        return {
            trade.contract.conId
            for trade in open_trades
            if isinstance(trade.contract, Option)
            and trade.contract.right.upper().startswith("P")
            and trade.contract.conId > 0
        }

    def _queued_put_con_ids(self) -> set[int]:
        return {
            contract.conId
            for contract, _order, _intent_id in self.order_ops.orders.records()
            if isinstance(contract, Option)
            and contract.right.upper().startswith("P")
            and contract.conId > 0
        }

    def _queued_tail_close_con_ids(self) -> set[int]:
        return {
            contract.conId
            for contract, order, _intent_id in self.order_ops.orders.records()
            if isinstance(contract, Option)
            and contract.conId > 0
            and is_tail_reduction_ref(getattr(order, "orderRef", None))
        }

    def _same_run_stock_trade_symbols(self) -> set[str]:
        return {
            contract.symbol
            for contract, order, _intent_id in self.order_ops.orders.records()
            if isinstance(contract, Stock)
            and str(getattr(order, "action", "")).upper() in {"BUY", "SELL"}
        }

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

    def _record_evaluation(
        self,
        outcome: str,
        *,
        symbol: Optional[str] = None,
        **payload: Any,
    ) -> None:
        if symbol is not None:
            self._run_outcomes[symbol] = outcome
        if self.data_store is None:
            return
        self.data_store.record_event(
            TAIL_HEDGE_EVALUATION_EVENT,
            {
                "schema_version": TAIL_HEDGE_EVALUATION_SCHEMA_VERSION,
                "account": self.config.runtime.account.number,
                "evaluated_at": self._now(),
                "symbol": symbol,
                "outcome": outcome,
                **payload,
            },
            symbol=symbol,
        )

    def _log_program_summary(
        self,
        state: TailHedgeState,
        *,
        net_liquidation: float,
    ) -> None:
        now = self._now()
        recent_ids = {cohort.entry_id for cohort in state.recent_cohorts(now)}
        budget_cohorts = [
            cohort
            for cohort in state.cohorts
            if cohort.entry_id in recent_ids or cohort.is_open
        ]
        global_budget = net_liquidation * float(
            self.config.strategies.tail_hedge.annual_budget
        )
        global_spent = sum(cohort.net_charge for cohort in budget_cohorts)

        for target in self.config.strategies.tail_hedge.targets:
            symbol_cohorts = [
                cohort
                for cohort in state.open_cohorts
                if cohort.symbol == target.symbol
            ]
            pending_entries = sum(
                cohort.status == "entry_enqueued" for cohort in symbol_cohorts
            )
            target_spent = sum(
                cohort.net_charge
                for cohort in budget_cohorts
                if cohort.symbol == target.symbol
            )
            target_budget = global_budget * target.budget_weight
            entered_at = [
                cohort.entered_at
                for cohort in state.cohorts
                if cohort.symbol == target.symbol
            ]
            next_entry = "now"
            if entered_at:
                eligible_at = max(entered_at) + timedelta(
                    days=target.minimum_entry_spacing_days
                )
                if eligible_at > now:
                    next_entry = eligible_at.date().isoformat()
            outcome = self._run_outcomes.get(target.symbol, "no_action")
            log.info(
                f"{target.symbol}: Tail hedge summary: outcome={outcome}; "
                f"open_cohorts={len(symbol_cohorts)} "
                f"(entry_pending={pending_entries}); "
                f"annual_estimated_cost={dfmt(target_spent)}/{dfmt(target_budget)} "
                f"target, {dfmt(global_spent)}/{dfmt(global_budget)} global; "
                f"next_entry={next_entry}."
            )

    def _require_state_store(self) -> TailHedgeStateStore:
        if self.state_store is None:
            raise RuntimeError("Tail hedge requires SQLite state storage.")
        return self.state_store

    def _dte(self, expiration: str) -> int:
        return (contract_date_to_datetime(expiration).date() - self._now().date()).days

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
        if not contract.multiplier:
            raise RuntimeError("Put contract multiplier is unavailable")
        multiplier = float(contract.multiplier)
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
