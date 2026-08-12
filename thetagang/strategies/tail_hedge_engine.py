from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from ib_async import ExecutionFilter, PortfolioItem, Ticker, util
from ib_async.contract import ComboLeg, Contract, Index, Option, Stock

from thetagang import log
from thetagang.config import Config
from thetagang.db import DataStore
from thetagang.fmt import dfmt
from thetagang.ibkr import IBKR, RequiredFieldValidationError, TickerField
from thetagang.options import contract_date_to_datetime
from thetagang.trading_operations import OrderOperations
from thetagang.util import midpoint_or_market_price

TAIL_HEDGE_ENTRY_ORDER_REF = "tg:tail-hedge:entry"
TAIL_HEDGE_SHORT_CLOSE_ORDER_REF = "tg:tail-hedge:short-close"
TAIL_HEDGE_CLOSE_ORDER_REF = "tg:tail-hedge:close"
TAIL_HEDGE_EVALUATION_EVENT = "tail_hedge_evaluation"
TAIL_HEDGE_STATE_EVENT = "tail_hedge_state"
LOTTERY_TICKET_EXIT_DTE = 1
LOTTERY_STATE_FIELDS = (
    "lottery_long_con_id",
    "lottery_long_expiration",
    "lottery_long_quantity",
    "lottery_long_strike",
    "lottery_long_retained_at",
    "lottery_long_retained_bid",
)
TAIL_HEDGE_ORDER_REFS = frozenset(
    {
        TAIL_HEDGE_ENTRY_ORDER_REF,
        TAIL_HEDGE_SHORT_CLOSE_ORDER_REF,
        TAIL_HEDGE_CLOSE_ORDER_REF,
    }
)


@dataclass(frozen=True)
class SpreadQuote:
    expiration: str
    dte: int
    underlying_price: float
    long_con_id: int
    long_local_symbol: str
    long_strike: float
    long_bid: float
    long_ask: float
    long_open_interest: float
    short_con_id: int
    short_local_symbol: str
    short_strike: float
    short_bid: float
    short_ask: float
    short_open_interest: float
    midpoint_debit: float
    limit_debit: float
    spread_width: float
    debit_ratio: float
    long_bid_ask_ratio: float
    short_bid_ask_ratio: float


@dataclass(frozen=True)
class StockExposure:
    market_value: float
    market_price: Optional[float]


@dataclass(frozen=True)
class ManagedHedge:
    long_position: Optional[PortfolioItem]
    short_position: Optional[PortfolioItem]


@dataclass(frozen=True)
class UnderlyingQuote:
    contract: Contract
    price: float


class TailHedgeEngine:
    """Manage one put-spread tranche plus one near-worthless residual tranche."""

    def __init__(
        self,
        *,
        config: Config,
        ibkr: IBKR,
        order_ops: OrderOperations,
        data_store: Optional[DataStore],
        qualified_contracts: Dict[int, Contract],
        now_provider: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.order_ops = order_ops
        self.data_store = data_store
        self.qualified_contracts = qualified_contracts
        self._now = now_provider

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

        symbol = tail_config.symbol
        log.notice(f"{symbol}: Evaluating tail-hedge put spread...")

        try:
            await self._refresh_execution_telemetry(symbol)
            await self._manage_positions(
                portfolio_positions.get(symbol, []),
                net_liquidation=net_liquidation,
            )
        except (
            IndexError,
            RequiredFieldValidationError,
            RuntimeError,
            StopIteration,
            TypeError,
            ValueError,
        ) as exc:
            self._record_evaluation(
                "evaluation_error",
                error_type=type(exc).__name__,
                detail=str(exc),
            )
            log.error(
                f"{symbol}: Tail-hedge evaluation failed ({type(exc).__name__}): {exc}"
            )

    async def _refresh_execution_telemetry(self, symbol: str) -> None:
        try:
            start = self._now() - timedelta(days=7)
            exec_filter = ExecutionFilter(time=start.strftime("%Y%m%d %H:%M:%S"))
            await self.ibkr.request_executions(exec_filter)
        except RuntimeError as exc:
            log.warning(
                f"{symbol}: Could not refresh recent executions "
                f"({type(exc).__name__}); continuing with persisted state."
            )
            if self.data_store is not None:
                self.data_store.record_event(
                    "tail_hedge_execution_refresh_failed",
                    {
                        "schema_version": 1,
                        "error_type": type(exc).__name__,
                        "detail": str(exc),
                    },
                    symbol=symbol,
                )

    async def _manage_positions(
        self,
        symbol_positions: List[PortfolioItem],
        *,
        net_liquidation: float,
    ) -> None:
        if self.data_store is None:
            raise RuntimeError("Tail hedge requires SQLite state storage.")

        symbol = self.config.strategies.tail_hedge.symbol
        working_orders = self._working_tail_orders(symbol)
        if working_orders:
            self._record_evaluation("working_order_present", orders=working_orders)
            order_refs = ", ".join(order["order_ref"] for order in working_orders)
            log.notice(
                f"{symbol}: Tail-hedge order still working ({order_refs}); holding."
            )
            return

        put_positions = [
            position
            for position in symbol_positions
            if isinstance(position.contract, Option)
            and position.contract.right.upper().startswith("P")
            and not math.isclose(float(position.position), 0.0)
        ]
        state = self.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
        lottery_long = self._resolve_lottery_long(put_positions, state)
        if lottery_long is None:
            state = self._without_lottery_state(state)
        elif await self._close_expiring_lottery_long(lottery_long, state):
            return

        lottery_con_id = (
            lottery_long.contract.conId if lottery_long is not None else None
        )
        active_put_positions = [
            position
            for position in put_positions
            if position.contract.conId != lottery_con_id
        ]
        # Persisted state identifies strategy-owned contracts and the last order
        # intent; it is not evidence that an order filled. Reconcile it against
        # the broker's current positions on every independent daily run.
        managed_hedge = self._resolve_managed_hedge(active_put_positions, state)
        stock_exposure = self._stock_exposure(symbol_positions)

        if managed_hedge is not None and state is not None:
            lottery_state = await self._manage_existing_hedge(
                managed_hedge,
                state,
                underlying_price=stock_exposure.market_price,
                may_retain_long=lottery_long is None,
            )
            if lottery_state is not None:
                await self._evaluate_entry(
                    stock_exposure,
                    net_liquidation=net_liquidation,
                    previous_state=lottery_state,
                )
            return

        if (
            not active_put_positions
            and state is not None
            and (
                state.get("long_con_id") is not None
                or state.get("short_con_id") is not None
            )
        ):
            previous_status = state.get("status")
            previous_order_ref = state.get("order_ref")
            state = self._record_state(
                "no_active_hedge",
                previous=self._lottery_state(state),
                long_con_id=None,
                short_con_id=None,
                reconciled_from_status=previous_status,
                reconciled_from_order_ref=previous_order_ref,
            )
            self._record_evaluation(
                "state_reconciled_no_active_position",
                previous_status=previous_status,
                previous_order_ref=previous_order_ref,
            )

        if active_put_positions:
            self._record_evaluation(
                "unmanaged_put_positions",
                con_ids=[position.contract.conId for position in active_put_positions],
            )
            log.warning(
                f"{symbol}: Existing puts are not owned by the tail-hedge strategy; "
                "refusing to add another spread."
            )
            return

        await self._evaluate_entry(
            stock_exposure,
            net_liquidation=net_liquidation,
            previous_state=self._lottery_state(state),
        )

    async def _evaluate_entry(
        self,
        stock_exposure: StockExposure,
        *,
        net_liquidation: float,
        previous_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.data_store is None:
            raise RuntimeError("Tail hedge requires SQLite state storage.")

        tail_config = self.config.strategies.tail_hedge
        symbol = tail_config.symbol
        protected_value = stock_exposure.market_value
        if protected_value <= 0:
            self._record_evaluation("no_protected_stock_position")
            return

        if not self._is_positive(net_liquidation):
            raise RuntimeError("Net liquidation value is unavailable")

        annual_budget = round(
            net_liquidation * float(tail_config.annual_budget),
            2,
        )
        budget_start = self._now() - timedelta(days=365)
        spent = self.data_store.get_filled_combo_debit(
            TAIL_HEDGE_ENTRY_ORDER_REF,
            budget_start,
            symbol=symbol,
        )
        remaining_budget = round(max(0.0, annual_budget - spent), 2)
        budget_details = {
            "net_liquidation": net_liquidation,
            "protected_position_value": protected_value,
            "annual_budget_rate": float(tail_config.annual_budget),
            "annual_budget": annual_budget,
            "rolling_entry_debit": spent,
            "remaining_budget": remaining_budget,
            "budget_window_start": budget_start,
        }
        if remaining_budget <= 0:
            self._record_evaluation("annual_budget_exhausted", **budget_details)
            return

        vix_ticker = await self.ibkr.get_ticker_for_contract(
            Index("VIX", "CBOE", "USD")
        )
        vix = float(vix_ticker.marketPrice())
        if not self._is_positive(vix):
            raise RuntimeError("VIX market price is unavailable")
        if vix > float(tail_config.entry_vix_max):
            self._record_evaluation(
                "vix_above_entry_max",
                vix=vix,
                entry_vix_max=float(tail_config.entry_vix_max),
                **budget_details,
            )
            return

        quote, long_contract, short_contract = await self._find_spread()
        quote_details = {"vix": vix, **budget_details, "quote": asdict(quote)}
        rejection = self._quote_rejection(quote)
        if rejection is not None:
            self._record_evaluation(rejection, **quote_details)
            return

        per_spread_cost = round(
            quote.limit_debit * self._multiplier(long_contract),
            2,
        )
        entry_quantity = max(1, math.floor(remaining_budget / per_spread_cost))
        entry_cost = round(per_spread_cost * entry_quantity, 2)
        budget_overage = round(max(0.0, entry_cost - remaining_budget), 2)

        combo = self._spread_contract(
            symbol,
            long_contract,
            short_contract,
            long_action="BUY",
            short_action="SELL",
        )
        order = self.order_ops.create_limit_order(
            action="BUY",
            quantity=entry_quantity,
            limit_price=quote.limit_debit,
            use_default_algo=False,
            order_ref=TAIL_HEDGE_ENTRY_ORDER_REF,
            transmit=True,
        )
        self.order_ops.enqueue_order(combo, order)
        self._record_state(
            "entry_enqueued",
            previous=previous_state,
            expiration=quote.expiration,
            long_con_id=quote.long_con_id,
            long_local_symbol=quote.long_local_symbol,
            long_strike=quote.long_strike,
            short_con_id=quote.short_con_id,
            short_local_symbol=quote.short_local_symbol,
            short_strike=quote.short_strike,
            short_entry_midpoint=(quote.short_bid + quote.short_ask) / 2.0,
            entry_quantity=entry_quantity,
            entry_limit_debit=quote.limit_debit,
            entry_cost=entry_cost,
            budget_overage=budget_overage,
            order_ref=TAIL_HEDGE_ENTRY_ORDER_REF,
        )
        self._record_evaluation(
            "entry_enqueued",
            entry_quantity=entry_quantity,
            per_spread_cost=per_spread_cost,
            entry_cost=entry_cost,
            budget_overage=budget_overage,
            **quote_details,
        )
        log.notice(
            f"{symbol}: Enqueued {entry_quantity}x "
            f"{quote.long_strike:g}/{quote.short_strike:g} put spreads expiring "
            f"{quote.expiration} at a {dfmt(quote.limit_debit)} debit each."
        )

    def _quote_rejection(self, quote: SpreadQuote) -> Optional[str]:
        tail_config = self.config.strategies.tail_hedge
        if min(quote.long_open_interest, quote.short_open_interest) < (
            tail_config.minimum_open_interest
        ):
            return "insufficient_open_interest"
        if min(quote.long_bid, quote.short_bid) < tail_config.minimum_bid:
            return "bid_below_minimum"
        if max(quote.long_bid_ask_ratio, quote.short_bid_ask_ratio) > (
            tail_config.max_bid_ask_ratio
        ):
            return "bid_ask_too_wide"
        if quote.debit_ratio > tail_config.max_debit_ratio:
            return "spread_too_expensive"
        return None

    def _working_tail_orders(self, symbol: str) -> List[Dict[str, Any]]:
        working_orders = []
        for trade in self.ibkr.open_trades():
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            order_ref = getattr(order, "orderRef", None)
            if (
                order_ref not in TAIL_HEDGE_ORDER_REFS
                or getattr(contract, "symbol", None) != symbol
            ):
                continue
            status = getattr(trade, "orderStatus", None)
            working_orders.append(
                {
                    "order_ref": order_ref,
                    "order_id": getattr(order, "orderId", None),
                    "perm_id": getattr(order, "permId", None),
                    "status": getattr(status, "status", None),
                    "filled": getattr(status, "filled", None),
                    "remaining": getattr(status, "remaining", None),
                    "limit_price": getattr(order, "lmtPrice", None),
                }
            )
        return working_orders

    def _resolve_managed_hedge(
        self,
        put_positions: List[PortfolioItem],
        state: Optional[Dict[str, Any]],
    ) -> Optional[ManagedHedge]:
        if not state or state.get("symbol") != self.config.strategies.tail_hedge.symbol:
            return None
        long_con_id = state.get("long_con_id")
        short_con_id = state.get("short_con_id")
        if long_con_id is None or short_con_id is None:
            return None

        by_con_id = {position.contract.conId: position for position in put_positions}
        long_position = by_con_id.get(int(long_con_id))
        short_position = by_con_id.get(int(short_con_id))
        if long_position is None and short_position is None:
            return None
        return ManagedHedge(long_position, short_position)

    def _resolve_lottery_long(
        self,
        put_positions: List[PortfolioItem],
        state: Optional[Dict[str, Any]],
    ) -> Optional[PortfolioItem]:
        if not state or state.get("symbol") != self.config.strategies.tail_hedge.symbol:
            return None
        con_id = state.get("lottery_long_con_id")
        if con_id is None:
            return None
        return next(
            (
                position
                for position in put_positions
                if position.contract.conId == int(con_id)
                and float(position.position) > 0
            ),
            None,
        )

    async def _manage_existing_hedge(
        self,
        managed_hedge: ManagedHedge,
        state: Dict[str, Any],
        *,
        underlying_price: Optional[float],
        may_retain_long: bool,
    ) -> Optional[Dict[str, Any]]:
        symbol = self.config.strategies.tail_hedge.symbol
        long_position = managed_hedge.long_position
        short_position = managed_hedge.short_position
        if long_position is not None and short_position is None:
            if float(long_position.position) < 0:
                await self._close_unsafe_short(
                    long_position,
                    state,
                    reason="recorded_long_is_short",
                )
                return None
            return await self._manage_remaining_long(
                long_position,
                state,
                may_retain=may_retain_long,
            )
        if (
            long_position is None
            and short_position is not None
            and float(short_position.position) < 0
        ):
            await self._close_unsafe_short(
                short_position,
                state,
                reason="long_leg_missing",
            )
            return None
        if (
            long_position is None
            or short_position is None
            or float(long_position.position) <= 0
            or float(short_position.position) >= 0
        ):
            unsafe_shorts = [
                position
                for position in (long_position, short_position)
                if position is not None and float(position.position) < 0
            ]
            if unsafe_shorts:
                for position in unsafe_shorts:
                    await self._close_unsafe_short(
                        position,
                        state,
                        reason="managed_leg_sign_mismatch",
                    )
                return None
            self._record_evaluation(
                "unexpected_managed_position",
                long_position=(
                    float(long_position.position) if long_position is not None else None
                ),
                short_position=(
                    float(short_position.position)
                    if short_position is not None
                    else None
                ),
            )
            log.error(
                f"{symbol}: Tail-hedge legs have invalid signs; refusing to trade."
            )
            return None

        long_quantity = self._position_quantity(long_position)
        short_quantity = self._position_quantity(short_position)
        if short_quantity > long_quantity:
            await self._close_unsafe_short(
                short_position,
                state,
                reason="short_quantity_exceeds_long",
            )
            return None

        expiration = long_position.contract.lastTradeDateOrContractMonth
        if expiration != short_position.contract.lastTradeDateOrContractMonth:
            await self._close_unsafe_short(
                short_position,
                state,
                reason="managed_expirations_do_not_match",
            )
            return None
        dte = self._dte(expiration)
        if dte <= self.config.strategies.tail_hedge.exit_dte:
            await self._close_spread(
                long_position,
                short_position,
                state,
                dte,
                long_quantity=long_quantity,
                short_quantity=short_quantity,
            )
            return None

        await self._manage_short_leg(
            long_position,
            short_position,
            state,
            expiration=expiration,
            dte=dte,
            underlying_price=underlying_price,
        )
        return None

    async def _close_unsafe_short(
        self,
        short_position: PortfolioItem,
        state: Dict[str, Any],
        *,
        reason: str,
    ) -> None:
        symbol = self.config.strategies.tail_hedge.symbol
        ticker = await self._option_ticker(short_position.contract)
        limit_price = round(max(self._midpoint(ticker), 0.01), 2)
        quantity = self._position_quantity(short_position)
        self._enqueue_single_option_order(
            short_position.contract,
            action="BUY",
            quantity=quantity,
            limit_price=limit_price,
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        )
        close_details = {
            "close_reason": reason,
            "expiration": short_position.contract.lastTradeDateOrContractMonth,
            "dte": self._dte(short_position.contract.lastTradeDateOrContractMonth),
            "short_strike": float(short_position.contract.strike),
            "short_quantity": quantity,
            "limit_price": limit_price,
        }
        self._record_state(
            "unsafe_short_close_enqueued",
            previous=state,
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
            **close_details,
        )
        self._record_evaluation(
            "unsafe_short_close_enqueued",
            **close_details,
        )
        log.warning(
            f"{symbol}: Unsafe tail-hedge short exposure ({reason}); "
            "buying back all remaining short puts."
        )

    async def _manage_short_leg(
        self,
        long_position: PortfolioItem,
        short_position: PortfolioItem,
        state: Dict[str, Any],
        *,
        expiration: str,
        dte: int,
        underlying_price: Optional[float],
    ) -> None:
        short_ticker = await self._option_ticker(short_position.contract)
        short_midpoint = self._midpoint(short_ticker)
        short_entry_price = self._position_average_price(short_position)
        if short_entry_price <= 0:
            short_entry_price = float(state.get("short_entry_midpoint", 0.0))
        if short_entry_price <= 0:
            raise RuntimeError("Short-leg entry price is unavailable")
        profit_fraction = 1.0 - (short_midpoint / short_entry_price)
        target_profit = float(self.config.strategies.tail_hedge.short_close_profit)
        short_strike = float(short_position.contract.strike)
        close_reasons = []
        if profit_fraction >= target_profit:
            close_reasons.append("profit_target")

        if underlying_price is None and not close_reasons:
            try:
                underlying_price = (await self._underlying_quote()).price
            except (
                RequiredFieldValidationError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                log.warning(
                    f"{short_position.contract.symbol}: Could not refresh the "
                    f"underlying price for short-leg management: {exc}"
                )

        spot_to_short_strike = (
            underlying_price / short_strike
            if underlying_price is not None and short_strike > 0
            else None
        )
        minimum_spot_ratio = float(
            self.config.strategies.tail_hedge.short_exit_min_spot_ratio
        )
        if (
            spot_to_short_strike is not None
            and spot_to_short_strike <= minimum_spot_ratio
        ):
            close_reasons.append("tail_risk_buffer")

        if close_reasons:
            short_close_limit = round(max(short_midpoint, 0.01), 2)
            short_quantity = self._position_quantity(short_position)
            estimated_financing_profit = (
                (short_entry_price - short_midpoint)
                * self._multiplier(short_position.contract)
                * short_quantity
            )
            close_details = {
                "short_quantity": short_quantity,
                "short_entry_price": short_entry_price,
                "short_close_limit": short_close_limit,
                "short_profit_fraction": profit_fraction,
                "short_close_reasons": close_reasons,
                "underlying_price": underlying_price,
                "spot_to_short_strike": spot_to_short_strike,
                "short_exit_min_spot_ratio": minimum_spot_ratio,
                "estimated_short_financing_profit": estimated_financing_profit,
            }
            self._enqueue_single_option_order(
                short_position.contract,
                action="BUY",
                quantity=short_quantity,
                limit_price=short_close_limit,
                order_ref=TAIL_HEDGE_SHORT_CLOSE_ORDER_REF,
            )
            self._record_state(
                "short_close_enqueued",
                previous=state,
                order_ref=TAIL_HEDGE_SHORT_CLOSE_ORDER_REF,
                **close_details,
            )
            self._record_evaluation(
                "short_close_enqueued",
                expiration=expiration,
                dte=dte,
                target_profit=target_profit,
                **close_details,
            )
            return

        self._record_evaluation(
            "existing_spread_held",
            expiration=expiration,
            dte=dte,
            long_strike=float(long_position.contract.strike),
            long_quantity=self._position_quantity(long_position),
            short_strike=float(short_position.contract.strike),
            short_quantity=self._position_quantity(short_position),
            short_entry_price=short_entry_price,
            short_midpoint=short_midpoint,
            short_profit_fraction=profit_fraction,
            target_profit=target_profit,
            underlying_price=underlying_price,
            spot_to_short_strike=spot_to_short_strike,
            short_exit_min_spot_ratio=minimum_spot_ratio,
        )

    async def _close_spread(
        self,
        long_position: PortfolioItem,
        short_position: PortfolioItem,
        state: Dict[str, Any],
        dte: int,
        *,
        long_quantity: int,
        short_quantity: int,
    ) -> None:
        symbol = self.config.strategies.tail_hedge.symbol
        expiration = long_position.contract.lastTradeDateOrContractMonth
        long_ticker, short_ticker = await self._spread_tickers(
            long_position.contract,
            short_position.contract,
        )
        long_midpoint = self._midpoint(long_ticker)
        short_midpoint = self._midpoint(short_ticker)
        close_price = round(short_midpoint - long_midpoint, 2)
        if math.isclose(close_price, 0.0):
            close_price = -0.01
        combo = self._spread_contract(
            symbol,
            long_position.contract,
            short_position.contract,
            long_action="SELL",
            short_action="BUY",
        )
        order = self.order_ops.create_limit_order(
            action="BUY",
            quantity=short_quantity,
            limit_price=close_price,
            use_default_algo=False,
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
            transmit=True,
        )
        self.order_ops.enqueue_order(combo, order)
        excess_long_quantity = long_quantity - short_quantity
        long_close_limit = (
            round(max(long_midpoint, 0.01), 2) if excess_long_quantity > 0 else None
        )
        if long_close_limit is not None:
            self._enqueue_single_option_order(
                long_position.contract,
                action="SELL",
                quantity=excess_long_quantity,
                limit_price=long_close_limit,
                order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
            )

        close_details = {
            "expiration": expiration,
            "dte": dte,
            "spread_quantity": short_quantity,
            "excess_long_quantity": excess_long_quantity,
            "excess_long_limit": long_close_limit,
            "limit_price": close_price,
            "long_midpoint": long_midpoint,
            "short_midpoint": short_midpoint,
        }
        self._record_state(
            "spread_close_enqueued",
            previous=state,
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
            **close_details,
        )
        self._record_evaluation(
            "spread_close_enqueued",
            **close_details,
        )

    async def _manage_remaining_long(
        self,
        long_position: PortfolioItem,
        state: Dict[str, Any],
        *,
        may_retain: bool,
    ) -> Optional[Dict[str, Any]]:
        expiration = long_position.contract.lastTradeDateOrContractMonth
        dte = self._dte(expiration)
        if float(long_position.position) <= 0:
            self._record_evaluation(
                "unexpected_long_position",
                position=float(long_position.position),
            )
            return None
        long_quantity = self._position_quantity(long_position)
        if dte > self.config.strategies.tail_hedge.exit_dte:
            self._record_evaluation(
                "long_put_held",
                expiration=expiration,
                dte=dte,
                long_strike=float(long_position.contract.strike),
                long_quantity=long_quantity,
                estimated_short_financing_profit=state.get(
                    "estimated_short_financing_profit"
                ),
            )
            return None

        ticker = await self._option_ticker(long_position.contract)
        bid = self._quoted_bid(ticker)
        if (
            may_retain
            and dte > LOTTERY_TICKET_EXIT_DTE
            and bid <= self.config.strategies.tail_hedge.minimum_bid
        ):
            retained_details = {
                "expiration": expiration,
                "dte": dte,
                "long_con_id": long_position.contract.conId,
                "long_strike": float(long_position.contract.strike),
                "long_quantity": long_quantity,
                "quoted_bid": bid,
                "retention_bid_max": float(
                    self.config.strategies.tail_hedge.minimum_bid
                ),
                "lottery_exit_dte": LOTTERY_TICKET_EXIT_DTE,
                "estimated_short_financing_profit": state.get(
                    "estimated_short_financing_profit"
                ),
            }
            retained_state = self._record_state(
                "lottery_long_retained",
                long_con_id=None,
                short_con_id=None,
                lottery_long_con_id=long_position.contract.conId,
                lottery_long_expiration=expiration,
                lottery_long_quantity=long_quantity,
                lottery_long_strike=float(long_position.contract.strike),
                lottery_long_retained_at=self._now(),
                lottery_long_retained_bid=bid,
                estimated_short_financing_profit=state.get(
                    "estimated_short_financing_profit"
                ),
            )
            self._record_evaluation(
                "lottery_long_retained",
                **retained_details,
            )
            return self._lottery_state(retained_state)

        limit_price = round(max(self._midpoint(ticker), 0.01), 2)
        self._enqueue_single_option_order(
            long_position.contract,
            action="SELL",
            quantity=long_quantity,
            limit_price=limit_price,
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        )
        close_details = {
            "expiration": expiration,
            "dte": dte,
            "long_quantity": long_quantity,
            "limit_price": limit_price,
            "estimated_short_financing_profit": state.get(
                "estimated_short_financing_profit"
            ),
        }
        self._record_state(
            "long_close_enqueued",
            previous=state,
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
            **close_details,
        )
        self._record_evaluation(
            "long_close_enqueued",
            **close_details,
        )
        return None

    async def _close_expiring_lottery_long(
        self,
        long_position: PortfolioItem,
        state: Optional[Dict[str, Any]],
    ) -> bool:
        expiration = long_position.contract.lastTradeDateOrContractMonth
        dte = self._dte(expiration)
        if dte > LOTTERY_TICKET_EXIT_DTE:
            return False

        ticker = await self._option_ticker(long_position.contract)
        limit_price = round(max(self._midpoint(ticker), 0.01), 2)
        quantity = self._position_quantity(long_position)
        self._enqueue_single_option_order(
            long_position.contract,
            action="SELL",
            quantity=quantity,
            limit_price=limit_price,
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        )
        close_details = {
            "lottery_long_expiration": expiration,
            "lottery_long_dte": dte,
            "lottery_long_con_id": long_position.contract.conId,
            "lottery_long_quantity": quantity,
            "lottery_long_strike": float(long_position.contract.strike),
            "lottery_long_close_limit": limit_price,
        }
        self._record_state(
            "lottery_long_close_enqueued",
            previous=state,
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
            **close_details,
        )
        self._record_evaluation(
            "lottery_long_close_enqueued",
            **close_details,
        )
        return True

    def _enqueue_single_option_order(
        self,
        contract: Contract,
        *,
        action: str,
        quantity: int,
        limit_price: float,
        order_ref: str,
    ) -> None:
        order = self.order_ops.create_limit_order(
            action=action,
            quantity=quantity,
            limit_price=limit_price,
            order_ref=order_ref,
            transmit=True,
        )
        self.order_ops.enqueue_order(contract, order)

    async def _option_ticker(self, contract: Contract) -> Ticker:
        return await self.ibkr.get_ticker_for_contract(
            contract,
            required_fields=[],
            optional_fields=[TickerField.MARKET_PRICE, TickerField.MIDPOINT],
        )

    async def _find_spread(self) -> tuple[SpreadQuote, Contract, Contract]:
        tail_config = self.config.strategies.tail_hedge
        symbol = tail_config.symbol
        exchange = self.order_ops.get_order_exchange()
        underlying = await self._underlying_quote()
        underlying_price = underlying.price

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
            if tail_config.min_dte <= dte <= tail_config.max_dte
        ]
        if not eligible_expirations:
            raise RuntimeError(
                "No option expiration is inside the configured DTE range"
            )
        expiration = min(
            eligible_expirations,
            key=lambda value: (
                abs(value[1] - tail_config.target_dte),
                -value[1],
            ),
        )[0]
        long_target = underlying_price * tail_config.long_strike_ratio
        short_target = underlying_price * tail_config.short_strike_ratio
        otm_strikes = [
            strike for strike in chain.strikes if 0 < float(strike) < underlying_price
        ]
        if not otm_strikes:
            raise RuntimeError("No out-of-the-money put strikes are available")
        candidate_strikes = set(
            sorted(otm_strikes, key=lambda strike: abs(strike - long_target))[:5]
        ) | set(sorted(otm_strikes, key=lambda strike: abs(strike - short_target))[:5])
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
                for strike in sorted(candidate_strikes)
            ]
        )
        candidate_pairs = [
            (long_contract, short_contract)
            for long_contract in contracts
            for short_contract in contracts
            if float(short_contract.strike) < float(long_contract.strike)
        ]
        if not candidate_pairs:
            raise RuntimeError("No valid put-spread strike pair could be qualified")
        long_contract, short_contract = min(
            candidate_pairs,
            key=lambda pair: abs(float(pair[0].strike) - long_target)
            + abs(float(pair[1].strike) - short_target),
        )
        long_ticker, short_ticker = await self._spread_tickers(
            long_contract,
            short_contract,
        )
        quote = self._build_quote(
            underlying_price,
            long_ticker,
            short_ticker,
        )
        return quote, long_contract, short_contract

    async def _underlying_quote(self) -> UnderlyingQuote:
        symbol = self.config.strategies.tail_hedge.symbol
        symbol_config = self.config.portfolio.symbols[symbol]
        ticker = await self.ibkr.get_ticker_for_stock(
            symbol,
            symbol_config.primary_exchange or "",
            self.order_ops.get_order_exchange(),
        )
        if ticker.contract is None:
            raise RuntimeError("Underlying contract is unavailable")
        price = float(midpoint_or_market_price(ticker))
        if not self._is_positive(price):
            raise RuntimeError("Underlying market price is unavailable")
        return UnderlyingQuote(ticker.contract, price)

    async def _spread_tickers(
        self,
        long_contract: Contract,
        short_contract: Contract,
    ) -> tuple[Ticker, Ticker]:
        tickers = await self.ibkr.get_tickers_for_contracts(
            self.config.strategies.tail_hedge.symbol,
            [long_contract, short_contract],
            generic_tick_list="101",
            required_fields=[],
            optional_fields=[
                TickerField.MARKET_PRICE,
                TickerField.OPEN_INTEREST,
                TickerField.MIDPOINT,
            ],
        )
        by_con_id = {
            ticker.contract.conId: ticker
            for ticker in tickers
            if ticker.contract is not None
        }
        long_ticker = by_con_id.get(long_contract.conId)
        short_ticker = by_con_id.get(short_contract.conId)
        if long_ticker is None or short_ticker is None:
            raise RuntimeError("One or more option spread quotes are unavailable")
        return long_ticker, short_ticker

    def _build_quote(
        self,
        underlying_price: float,
        long_ticker: Ticker,
        short_ticker: Ticker,
    ) -> SpreadQuote:
        if long_ticker.contract is None or short_ticker.contract is None:
            raise RuntimeError("Spread ticker contract is unavailable")
        long_bid = float(long_ticker.bid)
        long_ask = float(long_ticker.ask)
        short_bid = float(short_ticker.bid)
        short_ask = float(short_ticker.ask)
        for label, value in (
            ("long bid", long_bid),
            ("long ask", long_ask),
            ("short bid", short_bid),
            ("short ask", short_ask),
        ):
            if not self._is_finite(value) or value < 0:
                raise RuntimeError(f"{label} is unavailable")
        if long_ask < long_bid or short_ask < short_bid:
            raise RuntimeError("Option quote is crossed")

        long_midpoint = (long_bid + long_ask) / 2.0
        short_midpoint = (short_bid + short_ask) / 2.0
        midpoint_debit = long_midpoint - short_midpoint
        if midpoint_debit <= 0:
            raise RuntimeError("Put spread does not have a positive midpoint debit")
        limit_debit = round(midpoint_debit, 2)
        if limit_debit <= 0:
            raise RuntimeError("Put spread midpoint is below the minimum price tick")

        spread_width = float(long_ticker.contract.strike) - float(
            short_ticker.contract.strike
        )
        if spread_width <= 0:
            raise RuntimeError("Put spread width must be positive")

        return SpreadQuote(
            expiration=long_ticker.contract.lastTradeDateOrContractMonth,
            dte=self._dte(long_ticker.contract.lastTradeDateOrContractMonth),
            underlying_price=underlying_price,
            long_con_id=long_ticker.contract.conId,
            long_local_symbol=long_ticker.contract.localSymbol,
            long_strike=float(long_ticker.contract.strike),
            long_bid=long_bid,
            long_ask=long_ask,
            long_open_interest=self._put_open_interest(long_ticker),
            short_con_id=short_ticker.contract.conId,
            short_local_symbol=short_ticker.contract.localSymbol,
            short_strike=float(short_ticker.contract.strike),
            short_bid=short_bid,
            short_ask=short_ask,
            short_open_interest=self._put_open_interest(short_ticker),
            midpoint_debit=midpoint_debit,
            limit_debit=limit_debit,
            spread_width=spread_width,
            debit_ratio=limit_debit / spread_width,
            long_bid_ask_ratio=self._bid_ask_ratio(long_bid, long_ask),
            short_bid_ask_ratio=self._bid_ask_ratio(short_bid, short_ask),
        )

    def _spread_contract(
        self,
        symbol: str,
        long_contract: Contract,
        short_contract: Contract,
        *,
        long_action: str,
        short_action: str,
    ) -> Contract:
        self.qualified_contracts[long_contract.conId] = long_contract
        self.qualified_contracts[short_contract.conId] = short_contract
        exchange = self.order_ops.get_order_exchange()
        return Contract(
            secType="BAG",
            symbol=symbol,
            currency="USD",
            exchange=exchange,
            comboLegs=[
                ComboLeg(
                    conId=long_contract.conId,
                    ratio=1,
                    exchange=exchange,
                    action=long_action,
                ),
                ComboLeg(
                    conId=short_contract.conId,
                    ratio=1,
                    exchange=exchange,
                    action=short_action,
                ),
            ],
        )

    def _stock_exposure(self, symbol_positions: List[PortfolioItem]) -> StockExposure:
        total_value = 0.0
        total_shares = 0.0
        for position in symbol_positions:
            if not isinstance(position.contract, Stock) or position.position <= 0:
                continue
            shares = float(position.position)
            market_value = float(getattr(position, "marketValue", 0.0) or 0.0)
            if not self._is_positive(market_value):
                market_price = float(getattr(position, "marketPrice", 0.0) or 0.0)
                if not self._is_positive(market_price):
                    continue
                market_value = shares * market_price
            total_shares += shares
            total_value += market_value

        market_price = total_value / total_shares if total_shares > 0 else None
        return StockExposure(total_value, market_price)

    def _record_state(
        self,
        status: str,
        *,
        previous: Optional[Dict[str, Any]] = None,
        **payload: Any,
    ) -> Dict[str, Any]:
        if self.data_store is None:
            raise RuntimeError("Tail hedge requires SQLite state storage.")
        symbol = self.config.strategies.tail_hedge.symbol
        recorded_at = self._now()
        state = {
            **(previous or {}),
            "schema_version": 1,
            "symbol": symbol,
            "status": status,
            "state_recorded_at": recorded_at,
            **payload,
        }
        if "order_ref" in payload:
            state["order_enqueued_at"] = recorded_at
        self.data_store.record_event(TAIL_HEDGE_STATE_EVENT, state, symbol=symbol)
        return state

    @staticmethod
    def _lottery_state(
        state: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not state or state.get("lottery_long_con_id") is None:
            return None
        return {key: state[key] for key in LOTTERY_STATE_FIELDS if key in state}

    @staticmethod
    def _without_lottery_state(
        state: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if state is None:
            return None
        return {
            key: value
            for key, value in state.items()
            if key not in LOTTERY_STATE_FIELDS
        }

    def _record_evaluation(self, outcome: str, **payload: Any) -> None:
        if self.data_store is None:
            return
        symbol = self.config.strategies.tail_hedge.symbol
        self.data_store.record_event(
            TAIL_HEDGE_EVALUATION_EVENT,
            {
                "schema_version": 1,
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
    def _quoted_bid(ticker: Ticker) -> float:
        value = float(ticker.bid)
        return value if TailHedgeEngine._is_finite(value) and value >= 0 else 0.0

    @staticmethod
    def _bid_ask_ratio(bid: float, ask: float) -> float:
        midpoint = (bid + ask) / 2.0
        if midpoint <= 0:
            return math.inf
        return (ask - bid) / midpoint

    @staticmethod
    def _multiplier(contract: Contract) -> float:
        return float(contract.multiplier or 100)

    @staticmethod
    def _position_average_price(position: PortfolioItem) -> float:
        average_cost = float(getattr(position, "averageCost", 0.0) or 0.0)
        return average_cost / TailHedgeEngine._multiplier(position.contract)

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
