from __future__ import annotations

import math
from dataclasses import asdict, dataclass
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
    TAIL_HEDGE_STATE_SCHEMA_VERSION,
    TailHedgeState,
    TailHedgeStateStore,
    is_tail_order_ref,
    is_tail_reduction_ref,
    parse_state_datetime,
)
from thetagang.trading_operations import OrderOperations
from thetagang.util import midpoint_or_market_price

TAIL_HEDGE_EVALUATION_EVENT = "tail_hedge_evaluation"
TAIL_HEDGE_ERRORS = (
    IndexError,
    RequestError,
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


class NoLaterExpirationError(RuntimeError):
    """Raised when a target's put ladder cannot extend to a later expiration."""


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
        dry_run: bool = False,
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
                now_provider=now_provider,
            )
            if data_store is not None
            else None
        )
        self.dry_run = dry_run
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
        if self.state_store is None:
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
        if self.state_store is None:
            raise RuntimeError("Tail hedge requires SQLite state storage.")

        state = self.state_store.load()
        open_trades = self._account_open_trades()
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
        targets = {
            target.symbol: target
            for target in self.config.strategies.tail_hedge.targets
        }

        put_positions = self._account_put_positions_by_con_id()
        removed_entry_ids: list[str] = []
        reconciled_tranches: list[dict[str, Any]] = []
        for tranche in state.tranches:
            con_id = int(tranche["con_id"])
            symbol = str(tranche["symbol"])
            position = put_positions.get(con_id)
            if position is None:
                matching_con_ids = {
                    trade.contract.conId
                    for trade in entry_trades
                    if trade.contract.symbol == symbol
                    if trade.contract.conId > 0
                }
                if (
                    tranche.get("status") == "entry_enqueued"
                    and (not matching_con_ids or con_id in matching_con_ids)
                    and any(trade.contract.symbol == symbol for trade in entry_trades)
                ):
                    reconciled_tranches.append(tranche)
                    continue
                removed_entry_ids.append(str(tranche["entry_id"]))
                continue

            reconciled = dict(tranche)
            if (
                float(position.position) > 0
                and reconciled["status"] == "entry_enqueued"
            ):
                reconciled["status"] = "active"
            if float(position.position) > 0:
                reconciled["quantity"] = self._position_quantity(position)
            reconciled_tranches.append(reconciled)

        if reconciled_tranches != state.tranches:
            state.tranches[:] = reconciled_tranches
            self.state_store.save(
                state,
                "reconciled",
                removed_entry_ids=removed_entry_ids,
                persistence_required=False,
            )

        blocked_entry_symbols = {
            trade.contract.symbol for trade in close_trades
        } | self._same_run_regime_trade_symbols()
        for tranche in state.tranches:
            con_id = int(tranche["con_id"])
            symbol = str(tranche["symbol"])
            position = self._account_put_positions_by_con_id().get(con_id)
            if position is None:
                continue
            if con_id in pending_close_con_ids or not self.config.trading_is_allowed(
                symbol
            ):
                blocked_entry_symbols.add(symbol)
                continue
            try:
                close_enqueued = await self._manage_existing_put(
                    position,
                    targets.get(symbol),
                    tranche,
                )
            except TAIL_HEDGE_ERRORS as exc:
                blocked_entry_symbols.add(symbol)
                self._record_error(symbol, exc)
            else:
                if close_enqueued:
                    blocked_entry_symbols.add(symbol)

        occupied_con_ids = set(self._account_put_positions_by_con_id())
        occupied_con_ids |= self._queued_put_con_ids()
        occupied_con_ids |= self._working_put_con_ids(open_trades)
        working_entry_symbols = {trade.contract.symbol for trade in entry_trades}
        for target in self.config.strategies.tail_hedge.targets:
            symbol = target.symbol
            if symbol in blocked_entry_symbols or symbol in working_entry_symbols:
                if symbol in working_entry_symbols:
                    self._record_evaluation("working_order_present", symbol=symbol)
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
        tranche: dict[str, Any],
    ) -> bool:
        symbol = str(tranche["symbol"])
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
                entry_id=tranche["entry_id"],
                con_id=position.contract.conId,
            )
            return False

        await self._close_position(
            position,
            tranche,
            action=action,
            close_reason=reason,
        )
        return True

    async def _close_position(
        self,
        position: PortfolioItem,
        tranche: dict[str, Any],
        *,
        action: str,
        close_reason: str,
    ) -> None:
        symbol = str(tranche["symbol"])
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
                entry_id=tranche["entry_id"],
                con_id=position.contract.conId,
                expected_action=action,
                close_reason=close_reason,
            )
            return
        position = live_position
        quantity = self._position_quantity(position)
        position.contract.exchange = self.order_ops.get_order_exchange()
        order = self.order_ops.create_limit_order(
            action=action,
            quantity=quantity,
            limit_price=limit_price,
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
            transmit=True,
        )
        self.order_ops.enqueue_order(position.contract, order)

        self._record_evaluation(
            "close_enqueued",
            symbol=symbol,
            entry_id=tranche["entry_id"],
            con_id=position.contract.conId,
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
        state: TailHedgeState,
        occupied_con_ids: set[int],
    ) -> None:
        if self.state_store is None:
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
        recent_history = state.recent_entries(now)
        target_history = [
            entry for entry in recent_history if entry.get("symbol") == symbol
        ]
        if len(target_history) >= target.annual_tranches:
            self._record_evaluation("annual_tranche_limit", symbol=symbol)
            return

        entered_at = [
            value
            for entry in target_history
            if (value := parse_state_datetime(entry.get("entered_at"))) is not None
        ]
        if (
            entered_at
            and (now - max(entered_at)).days < target.minimum_tranche_spacing_days
        ):
            self._record_evaluation("minimum_entry_spacing", symbol=symbol)
            return

        global_spent = sum(float(entry["estimated_cost"]) for entry in recent_history)
        target_spent = sum(float(entry["estimated_cost"]) for entry in target_history)

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
                    target_budget / target.annual_tranches,
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

        latest_expiration = max(
            (
                str(tranche["expiration"])
                for tranche in state.tranches
                if tranche.get("symbol") == symbol
            ),
            default=None,
        )
        try:
            quote, contract = await self._find_put(
                target,
                latest_expiration=latest_expiration,
                exclude_con_ids=occupied_con_ids,
            )
        except NoLaterExpirationError:
            self._record_evaluation(
                "no_later_expiration_available",
                symbol=symbol,
                latest_expiration=latest_expiration,
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

        per_contract_cost = round(quote.limit_price * self._multiplier(contract), 2)
        quantity = math.floor(applicable_budget / per_contract_cost)
        if quantity < 1:
            self._record_evaluation(
                "contract_exceeds_applicable_budget",
                symbol=symbol,
                per_contract_cost=per_contract_cost,
            )
            return
        live_stock_exposure = self._stock_exposure(
            [
                position
                for position in self.ibkr.portfolio(
                    account=self.config.runtime.account.number
                )
                if position.contract.symbol == symbol
            ]
        )
        if live_stock_exposure <= 0 or symbol in self._same_run_regime_trade_symbols():
            self._record_evaluation("protected_position_changed", symbol=symbol)
            return
        entry_cost = round(per_contract_cost * quantity, 2)
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
            "entry_enqueued_at": entered_at,
        }
        history_entry = {
            "entry_id": entry_id,
            "symbol": symbol,
            "entered_at": entered_at,
            "estimated_cost": entry_cost,
        }
        state.roll_entry_history(now)
        state.tranches.append(tranche)
        state.entry_history.append(history_entry)
        self.state_store.save(
            state,
            "entry_enqueued",
            action_symbol=symbol,
            action_entry_id=entry_id,
            order_ref=TAIL_HEDGE_ENTRY_ORDER_REF,
        )
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
            quote=asdict(quote),
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
        latest_expiration: Optional[str],
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
        minimum_expiration = (
            contract_date_to_datetime(latest_expiration).date()
            + timedelta(days=target.minimum_tranche_spacing_days)
            if latest_expiration is not None
            else None
        )
        eligible_expirations = [
            (expiration, self._dte(expiration))
            for expiration in chain.expirations
            if target.min_dte <= self._dte(expiration) <= target.max_dte
            and (
                minimum_expiration is None
                or contract_date_to_datetime(expiration).date() >= minimum_expiration
            )
        ]
        if not eligible_expirations:
            if latest_expiration is not None:
                raise NoLaterExpirationError(
                    "No option expiration is inside the configured DTE range "
                    f"at least {target.minimum_tranche_spacing_days} days after "
                    f"{latest_expiration}"
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
        rejected_quotes: list[tuple[PutQuote, Contract]] = []
        for contract in contracts:
            ticker = tickers_by_con_id.get(contract.conId)
            if ticker is None:
                continue
            try:
                quote = self._build_quote(underlying.price, ticker)
            except (RuntimeError, TypeError, ValueError):
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

    def _account_open_trades(self) -> List[Trade]:
        account_number = self.config.runtime.account.number
        return [
            trade
            for trade in self.ibkr.open_trades()
            if getattr(getattr(trade, "order", None), "account", None) == account_number
        ]

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

    def _same_run_regime_trade_symbols(self) -> set[str]:
        return {
            contract.symbol
            for contract, order, _intent_id in self.order_ops.orders.records()
            if isinstance(contract, Stock)
            and str(getattr(order, "action", "")).upper() in {"BUY", "SELL"}
            and str(getattr(order, "orderRef", "")).startswith("tg:regime-rebalance:")
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
