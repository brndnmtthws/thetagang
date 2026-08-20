import asyncio
import math
from collections.abc import Awaitable, Callable, Coroutine
from enum import Enum
from typing import Any, cast

from ib_async import (
    IB,
    AccountValue,
    BarDataList,
    Contract,
    ExecutionFilter,
    Fill,
    Index,
    OptionChain,
    Order,
    PortfolioItem,
    Position,
    Stock,
    Ticker,
    Trade,
    util,
)
from ib_async.wrapper import RequestError
from rich.console import Console

from thetagang import log
from thetagang.db import DataStore

console = Console()

MAX_CONCURRENT_MARKET_DATA_STREAMS = 50


class TickerField(Enum):
    MIDPOINT = "midpoint"
    MARKET_PRICE = "market_price"
    GREEKS = "greeks"
    OPEN_INTEREST = "open_interest"


class RequiredFieldValidationError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)


BROKER_REQUEST_ERRORS = (
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    RequestError,
    RequiredFieldValidationError,
)


class IBKR:
    def __init__(
        self,
        ib: IB,
        api_response_wait_time: int,
        default_order_exchange: str,
        data_store: DataStore | None = None,
        dry_run: bool = False,
    ) -> None:
        self.ib = ib
        self.ib.orderStatusEvent += self.orderStatusEvent
        self.api_response_wait_time = api_response_wait_time
        self.default_order_exchange = default_order_exchange
        self.data_store = data_store
        self.dry_run = dry_run
        self.__market_data_semaphore = asyncio.Semaphore(
            MAX_CONCURRENT_MARKET_DATA_STREAMS
        )
        self.__market_data_contract_locks: dict[int, asyncio.Lock] = {}

    def portfolio(self, account: str) -> list[PortfolioItem]:
        return self.ib.portfolio(account)

    def cached_net_liquidation(self, account: str) -> float:
        """Read NLV from a newly materialized synchronized account-value cache."""
        net_liquidation = self.cached_account_value(account, "NetLiquidation")
        if net_liquidation > 0:
            return net_liquidation
        raise RuntimeError("Net liquidation value is unavailable")

    def cached_account_value(self, account: str, tag: str) -> float:
        """Read one finite value from ib_async's synchronized account cache."""
        candidates: list[tuple[str, str, float]] = []
        for value in self.ib.accountValues(account):
            if value.tag != tag:
                continue
            try:
                numeric_value = float(value.value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric_value):
                candidates.append(
                    (
                        str(value.currency).upper(),
                        str(getattr(value, "modelCode", "") or ""),
                        numeric_value,
                    )
                )

        aggregate_base_values = [
            numeric_value
            for currency, model_code, numeric_value in candidates
            if currency == "BASE" and not model_code
        ]
        if len(aggregate_base_values) == 1:
            return aggregate_base_values[0]
        if len(aggregate_base_values) > 1:
            raise RuntimeError(f"{tag} account value is unavailable")

        base_values = [
            numeric_value
            for currency, _model_code, numeric_value in candidates
            if currency == "BASE"
        ]
        if len(base_values) == 1:
            return base_values[0]
        if len(base_values) > 1:
            raise RuntimeError(f"{tag} account value is unavailable")
        if len(candidates) == 1:
            return candidates[0][2]
        raise RuntimeError(f"{tag} account value is unavailable")

    async def account_summary(self, account: str) -> list[AccountValue]:
        return await self.ib.accountSummaryAsync(account)

    async def refresh_account(self, account: str) -> None:
        """Restart account updates and wait for a fresh full snapshot."""
        self.ib.client.reqAccountUpdates(False, account)
        await self.ib.reqAccountUpdatesAsync(account)
        for value in self.ib.accountValues(account):
            if str(getattr(value, "tag", "")).lower() == "accountready" and str(
                getattr(value, "value", "")
            ).lower() in {"false", "0"}:
                raise RuntimeError("IBKR account snapshot is not ready")

    async def request_historical_data(
        self,
        contract: Contract,
        duration: str,
    ) -> BarDataList:
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            "",
            duration,
            "1 day",
            "TRADES",
            True,
        )
        if self.data_store:
            self.data_store.record_historical_bars(contract.symbol, "1 day", bars)
        return bars

    async def request_executions(
        self,
        exec_filter: ExecutionFilter | None = None,
    ) -> list[Fill]:
        fills = await self.ib.reqExecutionsAsync(exec_filter)
        if self.data_store:
            self.data_store.record_executions(fills)
        return fills

    def set_market_data_type(
        self,
        data_type: int,
    ) -> None:
        self.ib.reqMarketDataType(data_type)

    def open_trades(self) -> list[Trade]:
        return self.ib.openTrades()

    def trades(self) -> list[Trade]:
        """Return open and completed trades loaded for this broker session."""
        return self.ib.trades()

    def place_order(self, contract: Contract, order: Order) -> Trade:
        return self.ib.placeOrder(contract, order)

    def cancel_order(self, order: Order) -> None:
        if self.dry_run:
            log.warning("Dry run enabled, skipping broker order cancellation.")
            return
        self.ib.cancelOrder(order)

    def positions(self, account: str) -> list[Position]:
        return self.ib.positions(account)

    async def get_chains_for_contract(self, contract: Contract) -> list[OptionChain]:
        return await self.ib.reqSecDefOptParamsAsync(
            contract.symbol, "", contract.secType, contract.conId
        )

    async def qualify_contracts(self, *contracts: Contract) -> list[Contract]:
        results = await asyncio.gather(
            *(self.ib.qualifyContractsAsync(contract) for contract in contracts),
            return_exceptions=True,
        )
        qualified: list[Contract] = []
        for requested_contract, result in zip(contracts, results):
            if isinstance(result, RequestError):
                if result.code != 200:
                    raise result
                log.warning(
                    f"Skipping invalid contract {requested_contract}: {result.message}"
                )
                continue
            if isinstance(result, BaseException):
                raise result
            qualified.extend(
                cast(Contract, contract) for contract in result if contract is not None
            )
        return qualified

    async def get_ticker_for_stock(
        self,
        symbol: str,
        primary_exchange: str,
        order_exchange: str | None = None,
        generic_tick_list: str = "",
        required_fields: list[TickerField] | None = None,
        optional_fields: list[TickerField] | None = None,
    ) -> Ticker:
        if optional_fields is None:
            optional_fields = [TickerField.MIDPOINT]
        if required_fields is None:
            required_fields = [TickerField.MARKET_PRICE]
        stock = Stock(
            symbol,
            order_exchange or self.default_order_exchange,
            currency="USD",
            primaryExchange=primary_exchange,
        )
        qualified = await self.qualify_contracts(stock)
        contract: Contract = qualified[0] if qualified else stock

        if not contract.conId:
            # Some underlyings (e.g. SPX) are indices, not stocks.
            index_exchange = primary_exchange or "CBOE"
            index_contract = Index(symbol, index_exchange, "USD")
            qualified_index = await self.qualify_contracts(index_contract)
            if qualified_index:
                contract = qualified_index[0]

        return await self.get_ticker_for_contract(
            contract, generic_tick_list, required_fields, optional_fields
        )

    async def get_tickers_for_contracts(
        self,
        underlying_symbol: str,
        contracts: list[Contract],
        generic_tick_list: str = "",
        required_fields: list[TickerField] | None = None,
        optional_fields: list[TickerField] | None = None,
    ) -> list[Ticker]:
        if optional_fields is None:
            optional_fields = [TickerField.MIDPOINT]
        if required_fields is None:
            required_fields = [TickerField.MARKET_PRICE]

        async def get_ticker_task(contract: Contract) -> Ticker:
            return await self.get_ticker_for_contract(
                contract, generic_tick_list, required_fields, optional_fields
            )

        tasks: list[Coroutine[Any, Any, Ticker]] = [
            get_ticker_task(contract) for contract in contracts
        ]
        tickers = await log.track_async(
            tasks,
            description=f"{underlying_symbol}: Gathering tickers, waiting for required & optional fields...",
        )
        return tickers

    async def get_ticker_for_contract(
        self,
        contract: Contract,
        generic_tick_list: str = "",
        required_fields: list[TickerField] | None = None,
        optional_fields: list[TickerField] | None = None,
    ) -> Ticker:
        if optional_fields is None:
            optional_fields = [TickerField.MIDPOINT]
        if required_fields is None:
            required_fields = [TickerField.MARKET_PRICE]
        required_handlers = [
            (field, self.__ticker_field_handler__(field)) for field in required_fields
        ]
        optional_handlers = [
            (field, self.__ticker_field_handler__(field)) for field in optional_fields
        ]

        async def ticker_handler(ticker: Ticker) -> None:
            required_tasks = [handler(ticker) for _, handler in required_handlers]
            optional_tasks = [handler(ticker) for _, handler in optional_handlers]

            # Gather results, allowing optional tasks to potentially fail (timeout)
            results = await asyncio.gather(
                asyncio.gather(*required_tasks),
                asyncio.gather(
                    *optional_tasks, return_exceptions=False
                ),  # Don't raise exceptions here for optional
            )
            required_results = results[0]
            optional_results = results[1]

            # Check required results
            failed_required_fields = [
                field.name
                for i, (field, _) in enumerate(required_handlers)
                if not required_results[i]
            ]
            if failed_required_fields:
                raise RequiredFieldValidationError(
                    f"Required fields timed out for {contract.localSymbol}: {', '.join(failed_required_fields)}"
                )

            # Log warnings for optional results that timed out
            failed_optional_fields = [
                field.name
                for i, (field, _) in enumerate(optional_handlers)
                if not optional_results[i]
            ]
            if failed_optional_fields:
                log.warning(
                    f"Optional fields timed out for {contract.localSymbol}: {', '.join(failed_optional_fields)}"
                )

        return await self.__market_data_streaming_handler__(
            contract,
            generic_tick_list,
            lambda ticker: ticker_handler(ticker),
        )

    async def __wait_for_midpoint_price__(self, ticker: Ticker) -> bool:
        return await self.__ticker_wait_for_condition__(
            ticker, lambda t: not util.isNan(t.midpoint()), self.api_response_wait_time
        )

    async def __wait_for_market_price__(self, ticker: Ticker) -> bool:
        return await self.__ticker_wait_for_condition__(
            ticker,
            lambda t: not util.isNan(t.marketPrice()),
            self.api_response_wait_time,
        )

    async def __wait_for_greeks__(self, ticker: Ticker) -> bool:
        return await self.__ticker_wait_for_condition__(
            ticker,
            lambda t: (
                not (
                    t.modelGreeks is None
                    or t.modelGreeks.delta is None
                    or util.isNan(t.modelGreeks.delta)
                )
            ),
            self.api_response_wait_time,
        )

    async def __wait_for_open_interest__(self, ticker: Ticker) -> bool:
        def open_interest_is_not_ready(ticker: Ticker) -> bool:
            if not ticker.contract:
                return False
            if ticker.contract.right.startswith("P"):
                return util.isNan(ticker.putOpenInterest)
            else:
                return util.isNan(ticker.callOpenInterest)

        return await self.__ticker_wait_for_condition__(
            ticker,
            lambda t: not open_interest_is_not_ready(t),
            self.api_response_wait_time,
        )

    def orderStatusEvent(self, trade: Trade) -> None:
        if "Filled" in trade.orderStatus.status:
            log.info(f"{trade.contract.symbol}: Order filled")
        if "Fill" in trade.orderStatus.status:
            log.info(
                f"{trade.contract.symbol}: {trade.orderStatus.filled} filled, {trade.orderStatus.remaining} remaining"
            )
        if "Cancelled" in trade.orderStatus.status:
            log.warning(f"{trade.contract.symbol}: Order cancelled, trade={trade}")
        else:
            log.info(
                f"{trade.contract.symbol}: Order updated with status={trade.orderStatus.status}"
            )
        if self.data_store:
            self.data_store.record_order_status(trade)

    async def __market_data_streaming_handler__(
        self,
        contract: Contract,
        generic_tick_list: str,
        handler: Callable[[Ticker], Awaitable[Any]],
    ) -> Ticker:
        """
        Handles the streaming of market data for a given contract.

        This asynchronous method qualifies the contract, requests market data,
        and processes the data using the provided handler. Once the handler
        completes, the market data request is canceled.

        Args:
            contract (Contract): The contract for which market data is requested.
            handler (Callable[[Ticker], Awaitable[None]]): An asynchronous function
                that processes the received market data ticker.

        Returns:
            Ticker: The market data ticker for the given contract.
        """
        if not contract.conId:
            qualified = await self.qualify_contracts(contract)
            if qualified:
                contract = qualified[0]
        if not contract.conId:
            raise ValueError(
                f"Contract {contract} can't be qualified because no 'conId' value exists."
            )

        def active_ticker() -> Ticker | None:
            ticker = self.ib.ticker(contract)
            if ticker is None:
                return None

            request_id = self.ib.wrapper.ticker2ReqId.get("mktData", {}).get(ticker)
            return ticker if request_id is not None else None

        # Serialize same-contract access so a helper-owned stream cannot be reused
        # by another caller and then canceled while that caller is still handling it.
        contract_lock = self.__market_data_contract_locks.setdefault(
            contract.conId,
            asyncio.Lock(),
        )
        async with contract_lock:
            # Reuse a subscription owned elsewhere without canceling it. ib_async
            # keeps canceled tickers in its cache, so the request-id mapping is the
            # source of truth for whether a cached ticker still has an active stream.
            ticker = active_ticker()
            if ticker is not None:
                await handler(ticker)
                return ticker

            async with self.__market_data_semaphore:
                # Another contract may have freed stream capacity while this task
                # waited. Recheck in case an external owner subscribed meanwhile.
                ticker = active_ticker()
                if ticker is not None:
                    await handler(ticker)
                    return ticker

                ticker = self.ib.reqMktData(
                    contract,
                    genericTickList=generic_tick_list,
                )
                try:
                    await handler(ticker)
                    return ticker
                finally:
                    self.ib.cancelMktData(contract)

    async def __ticker_wait_for_condition__(
        self, ticker: Ticker, condition: Callable[[Ticker], bool], timeout: float
    ) -> bool:
        event = asyncio.Event()

        def onTicker(ticker: Ticker) -> None:
            if condition(ticker):
                event.set()

        update_event = ticker.updateEvent
        if update_event is None:
            raise RuntimeError("Ticker update event is unavailable")
        update_event += onTicker
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False
        finally:
            update_event -= onTicker

    async def wait_for_submitting_orders(
        self, trades: list[Trade], timetout: int = 60
    ) -> None:
        tasks: list[Coroutine[Any, Any, bool]] = [
            self.__trade_wait_for_condition__(
                trade,
                lambda trade: (
                    trade.orderStatus.status not in ["PendingSubmit", "PreSubmitted"]
                ),
                timetout,
            )
            for trade in trades
        ]
        results = await log.track_async(tasks, "Waiting for orders to be submitted...")
        if not all(results):
            failed_trades = [
                f"{trade.contract.symbol} (OrderId: {trade.order.orderId})"
                for i, trade in enumerate(trades)
                if not results[i]
            ]
            raise RuntimeError(
                f"Timeout waiting for orders to submit: {', '.join(failed_trades)}"
            )

    async def wait_for_orders_complete(
        self, trades: list[Trade], timetout: int = 60
    ) -> list[Trade]:
        tasks: list[Coroutine[Any, Any, bool]] = [
            self.__trade_wait_for_condition__(
                trade,
                lambda trade: trade.isDone(),
                timetout,
            )
            for trade in trades
        ]
        results = await log.track_async(
            tasks, description="Waiting for orders to complete..."
        )
        if not all(results):
            incomplete_trades = [
                trade for i, trade in enumerate(trades) if not results[i]
            ]
            incomplete_order_status = ", ".join(
                self._trade_progress_snapshot(trade) for trade in incomplete_trades
            )
            log.info(
                "Timeout waiting for orders to complete; "
                f"orders still working at broker: {incomplete_order_status}"
            )
            return incomplete_trades

        return []

    @staticmethod
    def _trade_progress_snapshot(trade: Trade) -> str:
        return (
            f"{trade.contract.symbol} (OrderId: {trade.order.orderId}, "
            f"status={getattr(trade.orderStatus, 'status', 'UNKNOWN')}, "
            f"filled={getattr(trade.orderStatus, 'filled', '?')}, "
            f"remaining={getattr(trade.orderStatus, 'remaining', '?')})"
        )

    async def __trade_wait_for_condition__(
        self, trade: Trade, condition: Callable[[Trade], bool], timeout: float
    ) -> bool:
        # perform an initial check first just incase Trade is in the correct condition
        # and onStatusEvent never gets triggered
        if condition(trade):
            return True

        event = asyncio.Event()

        def onStatusEvent(trade: Trade) -> None:
            if condition(trade):
                event.set()

        trade.statusEvent += onStatusEvent
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False
        finally:
            trade.statusEvent -= onStatusEvent

    def __ticker_field_handler__(
        self, ticker_field: TickerField
    ) -> Callable[[Ticker], Awaitable[bool]]:
        if ticker_field == TickerField.MIDPOINT:
            return self.__wait_for_midpoint_price__
        if ticker_field == TickerField.MARKET_PRICE:
            return self.__wait_for_market_price__
        if ticker_field == TickerField.GREEKS:
            return self.__wait_for_greeks__
        if ticker_field == TickerField.OPEN_INTEREST:
            return self.__wait_for_open_interest__
