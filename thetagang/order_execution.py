from __future__ import annotations

import asyncio
import copy
import math
import random
from typing import Any, Literal, TypeAlias, cast

import numpy as np
from ib_async import Contract, LimitOrder, MarketOrder, Order, Ticker
from ib_async.wrapper import RequestError

from thetagang import log
from thetagang.config import Config
from thetagang.config_models import SymbolConfig
from thetagang.db import DataStore
from thetagang.fmt import dfmt
from thetagang.ibkr import IBKR, RequiredFieldValidationError, TickerField
from thetagang.strategies.tail_hedge_state import (
    TAIL_HEDGE_MIN_LIMIT_PRICE_ATTR,
    is_tail_order_ref,
)
from thetagang.trades import Trades
from thetagang.util import would_increase_spread

PriceStrategy: TypeAlias = Literal["bid", "ask", "mid"]

# IB error codes that mark an order rejection: the broker destroyed the order
# without executing it. Verified against IB's documented error table
# (https://interactivebrokers.github.io/tws-api/message_codes.html):
# 201 "Order rejected - reason: ...". Extend this set from observed
# `order_error` events rather than guessing; unknown codes fail closed.
REJECT_ERROR_CODES: frozenset[int] = frozenset({201})
# IB error codes that report an external cancellation (by IBKR, the exchange,
# or a price/risk check) rather than a rejection: 202 "Order Canceled - ...".
CANCEL_ERROR_CODES: frozenset[int] = frozenset({202})
# TWS does not order status and error callbacks, so a rejection reason can
# arrive after the status. Before submitting a replacement for a rejected
# order, wait at most this long for the reason; paths that submit nothing
# record whatever reason is already present.
ORDER_ERROR_GRACE_SECONDS = 2.0


class OrderExecutionManager:
    """Apply opt-in pricing and supervise configured broker orders."""

    def __init__(
        self,
        config: Config,
        ibkr: IBKR,
        data_store: DataStore | None = None,
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.data_store = data_store

    def _policy_for_symbol(self, symbol: str) -> SymbolConfig.Execution | None:
        try:
            symbol_config = self.config.portfolio.symbols.get(symbol)
        except AttributeError:
            return None
        policy = getattr(symbol_config, "execution", None)
        if not isinstance(policy, SymbolConfig.Execution):
            return None
        if (
            policy.buy_price is None
            and policy.sell_price is None
            and policy.fill_timeout is None
        ):
            return None
        return policy

    @staticmethod
    def _strategy_for_order(
        policy: SymbolConfig.Execution,
        order: Order,
    ) -> PriceStrategy | None:
        action = str(getattr(order, "action", "")).upper()
        if action == "BUY":
            return policy.buy_price
        if action == "SELL":
            return policy.sell_price
        return None

    @staticmethod
    def _quote_is_valid(ticker: Ticker, price: Any) -> bool:
        try:
            value = float(price)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value):
            return False
        contract = getattr(ticker, "contract", None)
        if getattr(contract, "secType", None) == "BAG":
            return not math.isclose(value, 0.0, abs_tol=1e-12)
        return value > 0.0

    @classmethod
    def _price_from_ticker(
        cls,
        ticker: Ticker,
        strategy: PriceStrategy,
    ) -> float | None:
        if strategy == "bid":
            price = ticker.bid
        elif strategy == "ask":
            price = ticker.ask
        elif strategy == "mid":
            price = ticker.midpoint()
        else:
            return None
        return float(price) if cls._quote_is_valid(ticker, price) else None

    @staticmethod
    def _ticker_field(strategy: PriceStrategy) -> TickerField:
        if strategy == "bid":
            return TickerField.BID
        if strategy == "ask":
            return TickerField.ASK
        return TickerField.MIDPOINT

    def _round_limit_price(self, contract: Contract, price: float) -> float:
        if contract.symbol == "VIX":
            if price >= 3.0:
                return round(price * 20) / 20
            return round(price * 100) / 100
        return round(price, 2)

    def _apply_price_floor(
        self,
        contract: Contract,
        order: Order,
        price: float,
        *,
        include_minimum_credit: bool = False,
    ) -> float:
        action = str(getattr(order, "action", "")).upper()
        sec_type = getattr(contract, "secType", None)
        minimum = (
            getattr(order, TAIL_HEDGE_MIN_LIMIT_PRICE_ATTR, None)
            if action == "SELL"
            else None
        )
        if include_minimum_credit and sec_type in {"OPT", "BAG"}:
            minimum_credit = float(self.config.runtime.orders.minimum_credit)
            if action == "SELL" and price > 0.0:
                minimum = max(float(minimum or 0.0), minimum_credit)
            elif action == "BUY" and price < 0.0:
                return min(price, -minimum_credit)
        if isinstance(minimum, (int, float)) and math.isfinite(float(minimum)):
            return max(price, float(minimum))
        return price

    def _configured_limit_price(
        self,
        contract: Contract,
        order: Order,
        quoted_price: float,
    ) -> float:
        current_price = float(order.lmtPrice or 0.0)
        if (
            getattr(contract, "secType", None) == "BAG"
            and not math.isclose(current_price, 0.0, abs_tol=1e-12)
            and np.sign(current_price) != np.sign(quoted_price)
        ):
            raise RequiredFieldValidationError(
                f"configured quote changes the order sign for {contract.localSymbol}"
            )
        constrained_price = self._apply_price_floor(
            contract,
            order,
            quoted_price,
            include_minimum_credit=True,
        )
        return self._round_limit_price(contract, constrained_price)

    def _record_event(self, event_type: str, trade: Any, **details: Any) -> None:
        if self.data_store is None:
            return
        self.data_store.record_event(
            event_type,
            {
                "symbol": getattr(getattr(trade, "contract", None), "symbol", ""),
                "order_id": getattr(getattr(trade, "order", None), "orderId", None),
                "action": getattr(getattr(trade, "order", None), "action", None),
                **details,
            },
        )

    async def prepare_orders(
        self,
        records: list[tuple[Contract, LimitOrder, int | None]],
    ) -> None:
        """Apply configured initial prices without changing unconfigured orders."""

        async def prepare(
            record: tuple[Contract, LimitOrder, int | None],
        ) -> None:
            contract, order, _intent_id = record
            if is_tail_order_ref(getattr(order, "orderRef", None)):
                return
            policy = self._policy_for_symbol(contract.symbol)
            if policy is None:
                return
            strategy = self._strategy_for_order(policy, order)
            if strategy is None:
                return
            try:
                ticker = await self.ibkr.get_ticker_for_contract(
                    contract,
                    required_fields=[self._ticker_field(strategy)],
                    optional_fields=[],
                )
                configured_price = self._price_from_ticker(ticker, strategy)
                if configured_price is None:
                    raise RequiredFieldValidationError(
                        f"{strategy} quote is invalid for {contract.localSymbol}"
                    )
                configured_price = self._configured_limit_price(
                    contract,
                    order,
                    configured_price,
                )
                previous_price = float(order.lmtPrice or 0.0)
                order.lmtPrice = configured_price
                log.info(
                    f"{contract.symbol}: Applying configured {strategy} price "
                    f"to {order.action} order, old lmtPrice={dfmt(previous_price)} "
                    f"new lmtPrice={dfmt(configured_price)}"
                )
            except (
                TimeoutError,
                RequestError,
                RuntimeError,
                ValueError,
                RequiredFieldValidationError,
            ) as exc:
                log.warning(
                    f"{contract.symbol}: Couldn't apply configured {strategy} "
                    f"price; preserving lmtPrice={dfmt(float(order.lmtPrice or 0.0))}"
                )
                if self.data_store:
                    self.data_store.record_event(
                        "order_initial_price_strategy_skipped",
                        {
                            "symbol": contract.symbol,
                            "secType": contract.secType,
                            "strategy": strategy,
                            "reason": type(exc).__name__,
                        },
                    )

        await asyncio.gather(*(prepare(record) for record in records))

    @staticmethod
    def trade_fully_filled(trade: Any) -> bool:
        status = str(
            getattr(getattr(trade, "orderStatus", None), "status", "")
        ).casefold()
        try:
            filled = float(getattr(trade.orderStatus, "filled", 0) or 0)
            remaining = float(getattr(trade.orderStatus, "remaining", 0) or 0)
            requested = float(getattr(trade.order, "totalQuantity", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return False
        if (
            status != "filled"
            or not math.isfinite(requested)
            or requested <= 0
            or not math.isfinite(filled)
            or filled < requested
        ):
            return False
        return not math.isfinite(remaining) or math.isclose(
            remaining,
            0.0,
            abs_tol=1e-9,
        )

    async def reprice_trade(
        self,
        trades: Trades,
        idx: int,
        trade: Any,
        *,
        timeout: float | None = None,
        strategy: PriceStrategy | None = None,
    ) -> bool:
        response_timeout = self.config.runtime.ib_async.api_response_wait_time
        if timeout is not None:
            response_timeout = min(response_timeout, timeout)
        try:
            required_field = (
                self._ticker_field(strategy)
                if strategy is not None
                else TickerField.MIDPOINT
            )
            ticker = await asyncio.wait_for(
                self.ibkr.get_ticker_for_contract(
                    trade.contract,
                    required_fields=[required_field],
                    optional_fields=(
                        [TickerField.MARKET_PRICE] if strategy is None else []
                    ),
                ),
                timeout=response_timeout,
            )

            contract, order = trade.contract, trade.order
            old_price = float(order.lmtPrice or 0.0)
            if strategy is None:
                updated_price = np.sign(old_price) * max(
                    [
                        (
                            self.config.runtime.orders.minimum_credit
                            if order.action == "BUY" and old_price <= 0.0
                            else 0.0
                        ),
                        math.fabs(round((old_price + ticker.midpoint()) / 2.0, 2)),
                    ]
                )
            else:
                quoted_price = self._price_from_ticker(ticker, strategy)
                if quoted_price is None:
                    raise RequiredFieldValidationError(
                        f"{strategy} quote is invalid for {contract.localSymbol}"
                    )
                updated_price = self._configured_limit_price(
                    contract,
                    order,
                    quoted_price,
                )

            if strategy is None:
                updated_price = self._apply_price_floor(
                    contract,
                    order,
                    updated_price,
                )
                updated_price = self._round_limit_price(contract, updated_price)

            if would_increase_spread(order, updated_price):
                log.warning(
                    f"Skipping order for {contract.symbol}"
                    f" with old lmtPrice={dfmt(old_price)} "
                    f"updated lmtPrice={dfmt(updated_price)}, because updated "
                    "price would increase spread"
                )
                return False

            if old_price != updated_price and np.sign(old_price) == np.sign(
                updated_price
            ):
                log.info(
                    f"{contract.symbol}: Resubmitting {order.action} "
                    f"{contract.secType} order with old "
                    f"lmtPrice={dfmt(old_price)} "
                    f"updated lmtPrice={dfmt(updated_price)}"
                )
                updated_order = copy.deepcopy(cast(LimitOrder, order))
                updated_order.lmtPrice = float(updated_price)
                trades.submit_order(contract, updated_order, idx)
                log.info(f"{contract.symbol}: Order updated, order={updated_order}")
        except (
            TimeoutError,
            RequestError,
            RuntimeError,
            ValueError,
            RequiredFieldValidationError,
        ) as exc:
            log.warning(
                f"Couldn't generate execution price for {trade.contract}, "
                "skipping repricing"
            )
            self._record_event(
                "order_price_adjustment_skipped",
                trade,
                secType=getattr(trade.contract, "secType", ""),
                strategy=strategy or "legacy_midpoint",
                reason=type(exc).__name__,
            )
        return True

    def _legacy_adjustment_candidates(
        self,
        trades: Trades,
    ) -> list[tuple[int, Any]]:
        candidates: list[tuple[int, Any]] = []
        for idx, trade in enumerate(trades.records()):
            if not trade or is_tail_order_ref(getattr(trade.order, "orderRef", None)):
                continue
            symbol = trade.contract.symbol
            try:
                symbol_config = self.config.portfolio.symbols.get(symbol)
            except AttributeError:
                continue
            if (
                symbol_config is not None
                and self._policy_for_symbol(symbol) is None
                and symbol_config.adjust_price_after_delay
                and not trade.isDone()
            ):
                candidates.append((idx, trade))
        return candidates

    async def _adjust_legacy_prices(self, trades: Trades) -> None:
        try:
            adjustment_enabled = any(
                symbol_config.adjust_price_after_delay
                and self._policy_for_symbol(symbol) is None
                for symbol, symbol_config in self.config.portfolio.symbols.items()
            )
        except AttributeError:
            adjustment_enabled = False
        if not adjustment_enabled or trades.is_empty():
            log.warning("Skipping order price adjustments...")
            return

        delay = random.randrange(
            self.config.runtime.orders.price_update_delay[0],
            self.config.runtime.orders.price_update_delay[1],
        )
        await self.ibkr.wait_for_orders_complete(trades.records(), delay)

        for idx, current_trade in self._legacy_adjustment_candidates(trades):
            if not await self.reprice_trade(trades, idx, current_trade):
                return

    async def execute(self, trades: Trades) -> None:
        """Run legacy adjustment and opt-in per-symbol execution state machines."""

        supervised: list[tuple[int, Any, SymbolConfig.Execution]] = []
        for idx, trade in enumerate(trades.records()):
            if not trade or is_tail_order_ref(getattr(trade.order, "orderRef", None)):
                continue
            policy = self._policy_for_symbol(trade.contract.symbol)
            if policy is not None and policy.fill_timeout is not None:
                supervised.append((idx, trade, policy))

        tasks: list[Any] = [self._adjust_legacy_prices(trades)]
        tasks.extend(
            self._supervise_trade(trades, idx, trade, policy)
            for idx, trade, policy in supervised
        )
        await asyncio.gather(*tasks)

    async def _supervise_trade(
        self,
        trades: Trades,
        idx: int,
        trade: Any,
        policy: SymbolConfig.Execution,
    ) -> None:
        assert policy.fill_timeout is not None
        strategy = self._strategy_for_order(policy, trade.order)
        try:
            symbol_config = self.config.portfolio.symbols[trade.contract.symbol]
            legacy_reprice_enabled = symbol_config.adjust_price_after_delay
        except (AttributeError, KeyError):
            legacy_reprice_enabled = False

        loop = asyncio.get_running_loop()
        deadline = loop.time() + policy.fill_timeout
        wait_budget = float(policy.fill_timeout)
        legacy_repriced = False
        while wait_budget > 0:
            wait_budget = min(wait_budget, max(0.0, deadline - loop.time()))
            if wait_budget <= 0:
                break
            current_trade = trades.records()[idx]
            if self.trade_fully_filled(current_trade):
                return
            if current_trade.isDone():
                await self._handle_terminal_state(trades, idx, policy, current_trade)
                return

            should_reprice = strategy is not None or (
                legacy_reprice_enabled and not legacy_repriced
            )
            if should_reprice:
                delay = max(
                    1,
                    random.randrange(
                        self.config.runtime.orders.price_update_delay[0],
                        self.config.runtime.orders.price_update_delay[1],
                    ),
                )
                wait_time = min(delay, wait_budget)
            else:
                wait_time = wait_budget
            await self.ibkr.wait_for_orders_complete([current_trade], wait_time)
            wait_budget -= wait_time
            current_trade = trades.records()[idx]
            if self.trade_fully_filled(current_trade):
                return
            if current_trade.isDone():
                await self._handle_terminal_state(trades, idx, policy, current_trade)
                return

            if should_reprice:
                wait_budget = min(wait_budget, max(0.0, deadline - loop.time()))
                if wait_budget <= 0:
                    break
                await self.reprice_trade(
                    trades,
                    idx,
                    current_trade,
                    timeout=wait_budget,
                    strategy=strategy,
                )
                wait_budget = min(wait_budget, max(0.0, deadline - loop.time()))
                if strategy is None:
                    legacy_repriced = True

        await self._handle_timeout(trades, idx, policy)

    async def _handle_terminal_state(
        self,
        trades: Trades,
        idx: int,
        policy: SymbolConfig.Execution,
        trade: Any,
    ) -> None:
        """React to an order that reached a done state without a complete fill."""
        if self._is_inactive(trade):
            await self._handle_rejected(trades, idx, policy)
            return
        if self._is_cancelled(trade):
            await self._handle_cancelled(trades, idx, policy, trade)
            return
        log.warning(
            f"{trade.contract.symbol}: Configured order reached terminal "
            f"status={trade.orderStatus.status} without a complete fill."
        )

    @staticmethod
    def _is_cancelled(trade: Any) -> bool:
        status = str(
            getattr(getattr(trade, "orderStatus", None), "status", "")
        ).casefold()
        return status == "cancelled"

    async def _handle_cancelled(
        self,
        trades: Trades,
        idx: int,
        policy: SymbolConfig.Execution,
        trade: Any,
    ) -> None:
        """Classify a Cancelled order; replace it only when the broker rejected it.

        ib_async's wrapper synchronously marks an unfinished order Cancelled
        when a non-warning order error arrives (appending a log entry with the
        error code and emitting the reason on errorEvent in the same
        callback), so `order_error` is already populated by the time this
        supervisor observes the status. A rejection can therefore reach us as
        Cancelled with a REJECT_ERROR_CODES reason; ThetaGang's own
        cancellations and ApiCancelled carry no such reason and stay
        unreplaced, and unrecognized codes fail closed.
        """
        symbol = trade.contract.symbol
        error = self.ibkr.order_error(getattr(trade.order, "orderId", None))
        if error is None:
            log.warning(
                f"{symbol}: Configured order reached terminal status=Cancelled "
                "without a complete fill."
            )
            return
        code = error[0]
        if code in REJECT_ERROR_CODES:
            await self._handle_rejected(trades, idx, policy)
            return
        if code in CANCEL_ERROR_CODES:
            log.warning(
                f"{symbol}: Order was canceled by the broker (broker error "
                f"{error[0]}: {error[1]}); no replacement will be submitted."
            )
            return
        log.warning(
            f"{symbol}: Order ended Cancelled with unclassified broker "
            f"error {error[0]}: {error[1]}; no replacement will be submitted."
        )

    def _is_broker_rejected(self, trade: Any) -> bool:
        """True when the trade ended broker-rejected, not normally cancelled."""
        if self._is_inactive(trade):
            return True
        if not self._is_cancelled(trade):
            return False
        error = self.ibkr.order_error(getattr(trade.order, "orderId", None))
        return error is not None and error[0] in REJECT_ERROR_CODES

    async def _order_error_with_grace(
        self,
        order_id: Any,
    ) -> tuple[int, str] | None:
        """Fetch the broker error for an order, waiting briefly for late ones.

        Status and error callbacks are not ordered by TWS, so the reason for
        a rejection can arrive after the status. Call this only on paths that
        submit a replacement: wait at most ORDER_ERROR_GRACE_SECONDS for the
        reason so the rejection record carries it whenever the broker sent
        one. Paths that submit nothing should read `order_error` directly
        instead of holding the run.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ORDER_ERROR_GRACE_SECONDS
        while True:
            error = self.ibkr.order_error(order_id)
            if error is not None:
                return error
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.25, remaining))

    @staticmethod
    def _is_inactive(trade: Any) -> bool:
        status = str(
            getattr(getattr(trade, "orderStatus", None), "status", "")
        ).casefold()
        return status == "inactive"

    @staticmethod
    def _inactive_action(policy: SymbolConfig.Execution) -> str:
        action = policy.on_inactive
        if action is None:
            action = policy.on_timeout
        return action

    @staticmethod
    def _remaining_from_status(trade: Any) -> float | None:
        """Compute the unfilled quantity from order state without cancelling."""
        try:
            requested = float(trade.order.totalQuantity)
            filled = float(trade.orderStatus.filled or 0.0)
        except (AttributeError, TypeError, ValueError):
            return None
        if (
            not math.isfinite(requested)
            or requested <= 0
            or not math.isfinite(filled)
            or filled < 0
            or filled > requested
        ):
            return None
        return max(0.0, requested - filled)

    async def _handle_rejected(
        self,
        trades: Trades,
        idx: int,
        policy: SymbolConfig.Execution,
    ) -> None:
        """React to an order the broker rejected before it could finish.

        Two manifestations reach this path: the terminal ``Inactive`` status
        (destroyed by IBKR's risk management), or a ``Cancelled`` status that
        ib_async's wrapper wrote when a rejection error arrived first. Both
        are terminal at the broker: the order can never execute and ib_async
        drops it from openTrades, so there is nothing left to cancel and no
        point sending cancelOrder (it would only generate a spurious broker
        error). Submit at most one replacement for the unfilled remainder,
        then cancel whatever the replacement leaves behind.
        """
        trade = trades.records()[idx]
        symbol = trade.contract.symbol
        status = str(getattr(trade.orderStatus, "status", "Unknown"))
        why_held = getattr(trade.orderStatus, "whyHeld", None) or None
        order_id = getattr(trade.order, "orderId", None)
        action = self._inactive_action(policy)
        if action in ("leave_open", "cancel"):
            # No replacement will follow, so don't hold the run waiting for a
            # late error message; record whatever reason is already present.
            error = self.ibkr.order_error(order_id)
        else:
            error = await self._order_error_with_grace(order_id)
        reason_parts = []
        if error:
            reason_parts.append(f"broker error {error[0]}: {error[1]}")
        if why_held:
            reason_parts.append(f"whyHeld={why_held}")
        reason = f", {', '.join(reason_parts)}" if reason_parts else ""
        log.warning(
            f"{symbol}: Configured order was rejected by the broker without "
            f"a complete fill (status={status}{reason})."
        )
        remaining = self._remaining_from_status(trade)
        self._record_event(
            "order_broker_rejected",
            trade,
            status=status,
            rejection_action=action,
            remaining=remaining,
            error_code=error[0] if error else None,
            error_message=error[1] if error else None,
            why_held=why_held,
        )
        if remaining is None:
            log.error(
                f"{symbol}: Invalid fill quantities on rejected order; "
                "not submitting a replacement order."
            )
            return
        if math.isclose(remaining, 0.0, abs_tol=1e-9):
            log.warning(
                f"{symbol}: Rejected order reported a complete fill; "
                "not submitting a replacement order."
            )
            return
        if action == "leave_open":
            log.info(
                f"{symbol}: Leaving the rejected order's remainder "
                f"quantity={remaining:g} unreplaced."
            )
            return
        if action == "cancel":
            log.warning(
                f"{symbol}: Rejected order was already destroyed by the "
                f"broker; no replacement will be submitted for "
                f"quantity={remaining:g}."
            )
            return
        if action == "market":
            await self._submit_replacement(
                trades, idx, policy, remaining, "market", "Rejected"
            )
        else:
            await self._submit_replacement(
                trades, idx, policy, remaining, "marketable_limit", "Rejected"
            )

    @staticmethod
    def _cancellation_confirmed(trade: Any) -> bool:
        status = str(
            getattr(getattr(trade, "orderStatus", None), "status", "")
        ).casefold()
        return status in {"cancelled", "canceled", "apicancelled", "apicanceled"}

    async def _cancel_and_get_remaining(self, trade: Any) -> float | None:
        if self.trade_fully_filled(trade):
            return 0.0
        if not trade.isDone():
            self.ibkr.cancel_order(trade.order)
            incomplete = await self.ibkr.wait_for_orders_complete(
                [trade],
                max(
                    1,
                    min(60, self.config.runtime.ib_async.api_response_wait_time),
                ),
            )
            if incomplete:
                log.error(
                    f"{trade.contract.symbol}: Cancellation was not confirmed; "
                    "not submitting a replacement order."
                )
                return None

        if self.trade_fully_filled(trade):
            return 0.0
        if not self._cancellation_confirmed(trade):
            log.error(
                f"{trade.contract.symbol}: Order ended with "
                f"status={getattr(trade.orderStatus, 'status', 'UNKNOWN')}; "
                "not submitting a replacement order."
            )
            return None

        try:
            requested = float(trade.order.totalQuantity)
            filled = float(trade.orderStatus.filled or 0.0)
        except (AttributeError, TypeError, ValueError):
            return None
        if (
            not math.isfinite(requested)
            or requested <= 0
            or not math.isfinite(filled)
            or filled < 0
            or filled > requested
        ):
            log.error(
                f"{trade.contract.symbol}: Invalid fill quantities after "
                "cancellation; not submitting a replacement order."
            )
            return None
        return max(0.0, requested - filled)

    @staticmethod
    def _replacement_order_kwargs(order: Order, *, market: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "account": getattr(order, "account", ""),
            "tif": "DAY" if market else (getattr(order, "tif", "DAY") or "DAY"),
            "transmit": getattr(order, "transmit", True),
        }
        order_ref = getattr(order, "orderRef", None)
        if order_ref:
            kwargs["orderRef"] = order_ref
        if getattr(order, "outsideRth", False):
            kwargs["outsideRth"] = True
        return kwargs

    @staticmethod
    def _normalized_quantity(quantity: float) -> int | float:
        rounded = round(quantity)
        if math.isclose(quantity, rounded, abs_tol=1e-9):
            return int(rounded)
        return quantity

    async def _marketable_limit_order(
        self,
        trade: Any,
        quantity: float,
    ) -> LimitOrder | None:
        action = str(trade.order.action).upper()
        strategy = "ask" if action == "BUY" else "bid"
        try:
            ticker = await self.ibkr.get_ticker_for_contract(
                trade.contract,
                required_fields=[self._ticker_field(strategy)],
                optional_fields=[],
            )
            price = self._price_from_ticker(ticker, strategy)
            if price is None:
                raise RequiredFieldValidationError(
                    f"{strategy} quote is invalid for {trade.contract.localSymbol}"
                )
            price = self._configured_limit_price(
                trade.contract,
                trade.order,
                price,
            )
        except (
            TimeoutError,
            RequestError,
            RuntimeError,
            ValueError,
            RequiredFieldValidationError,
        ) as exc:
            log.error(
                f"{trade.contract.symbol}: Couldn't create marketable limit "
                f"replacement ({type(exc).__name__})."
            )
            return None
        return LimitOrder(
            action,
            self._normalized_quantity(quantity),
            price,
            **self._replacement_order_kwargs(trade.order, market=False),
        )

    async def _handle_timeout(
        self,
        trades: Trades,
        idx: int,
        policy: SymbolConfig.Execution,
    ) -> None:
        trade = trades.records()[idx]
        if self.trade_fully_filled(trade):
            return
        if policy.on_timeout == "leave_open":
            log.info(
                f"{trade.contract.symbol}: Fill timeout expired; leaving configured "
                "limit order open at the broker."
            )
            return

        remaining = await self._cancel_and_get_remaining(trade)
        if remaining is None or math.isclose(remaining, 0.0, abs_tol=1e-9):
            return
        self._record_event(
            "order_execution_timeout",
            trade,
            timeout_action=policy.on_timeout,
            remaining=remaining,
        )
        if policy.on_timeout == "cancel":
            log.warning(
                f"{trade.contract.symbol}: Fill timeout expired; canceled "
                f"remaining quantity={remaining:g}."
            )
            return

        if policy.on_timeout == "market":
            await self._submit_replacement(
                trades, idx, policy, remaining, "market", "Timeout"
            )
        else:
            await self._submit_replacement(
                trades, idx, policy, remaining, "marketable_limit", "Timeout"
            )

    async def _submit_replacement(
        self,
        trades: Trades,
        idx: int,
        policy: SymbolConfig.Execution,
        remaining: float,
        fallback: Literal["market", "marketable_limit"],
        label: str,
    ) -> None:
        """Submit one replacement order, then cancel what it leaves unfilled."""
        trade = trades.records()[idx]
        original_order = trade.order
        if fallback == "market":
            if getattr(trade.contract, "secType", None) == "BAG":
                log.error(
                    f"{trade.contract.symbol}: Market fallback is not supported "
                    "for combo orders; the unfilled remainder was canceled."
                )
                return
            replacement: Order | None = MarketOrder(
                str(original_order.action).upper(),
                self._normalized_quantity(remaining),
                **self._replacement_order_kwargs(original_order, market=True),
            )
        else:
            replacement = await self._marketable_limit_order(trade, remaining)

        if replacement is None:
            return
        if not trades.submit_order(trade.contract, replacement, idx):
            log.error(
                f"{trade.contract.symbol}: Failed to submit {label} replacement order."
            )
            return

        replacement_trade = trades.records()[idx]
        incomplete = await self.ibkr.wait_for_orders_complete(
            [replacement_trade],
            policy.final_wait,
        )
        replacement_trade = trades.records()[idx]
        if not incomplete and self.trade_fully_filled(replacement_trade):
            log.notice(
                f"{replacement_trade.contract.symbol}: {label} replacement order "
                "filled completely."
            )
            return
        if self._is_broker_rejected(replacement_trade):
            # No further replacement will be submitted, so don't wait for a
            # late error message; record whatever reason is already present.
            replacement_error = self.ibkr.order_error(
                getattr(replacement_trade.order, "orderId", None)
            )
            replacement_why_held = (
                getattr(replacement_trade.orderStatus, "whyHeld", None) or None
            )
            reasons = []
            if replacement_error:
                reasons.append(
                    f"broker error {replacement_error[0]}: {replacement_error[1]}"
                )
            if replacement_why_held:
                reasons.append(f"whyHeld={replacement_why_held}")
            detail = f" ({', '.join(reasons)})" if reasons else ""
            log.error(
                f"{replacement_trade.contract.symbol}: {label} replacement was "
                f"rejected by the broker{detail}; no further replacement will "
                "be submitted."
            )
            self._record_event(
                "order_replacement_rejected",
                replacement_trade,
                status=str(getattr(replacement_trade.orderStatus, "status", "Unknown")),
                error_code=replacement_error[0] if replacement_error else None,
                error_message=(replacement_error[1] if replacement_error else None),
                why_held=replacement_why_held,
            )
            return
        remaining_after_replacement = await self._cancel_and_get_remaining(
            replacement_trade
        )
        if remaining_after_replacement is None:
            log.error(
                f"{replacement_trade.contract.symbol}: {label} replacement did "
                "not fill completely and cancellation could not be confirmed."
            )
        else:
            log.error(
                f"{replacement_trade.contract.symbol}: {label} replacement did "
                "not fill completely; canceled remaining "
                f"quantity={remaining_after_replacement:g}."
            )
