import asyncio
from enum import Enum
from typing import Any, Awaitable, Callable, List, Optional

from ib_async import (
    IB,
    AccountValue,
    BarDataList,
    Contract,
    OptionChain,
    Order,
    PortfolioItem,
    Stock,
    Ticker,
    Trade,
    util,
)
from rich.console import Console

from thetagang import log

console = Console()


class TickerField(Enum):
    MIDPOINT = "midpoint"
    MARKET_PRICE = "market_price"
    GREEKS = "greeks"
    OPEN_INTEREST = "open_interest"


class RequiredFieldValidationError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)


class IBKR:
    def __init__(
        self, ib: IB, api_response_wait_time: int, default_order_exchange: str
    ) -> None:
        self.ib = ib
        self.ib.orderStatusEvent += self.orderStatusEvent
        self.api_response_wait_time = api_response_wait_time
        self.default_order_exchange = default_order_exchange

    def portfolio(self, account: str) -> List[PortfolioItem]:
        return self.ib.portfolio(account)

    async def account_summary(self, account: str) -> List[AccountValue]:
        return await self.ib.accountSummaryAsync(account)

    async def request_historical_data(
        self,
        contract: Contract,
        duration: str,
    ) -> BarDataList:
        return await self.ib.reqHistoricalDataAsync(
            contract,
            "",
            duration,
            "1 day",
            "TRADES",
            True,
        )

    def set_market_data_type(
        self,
        data_type: int,
    ) -> None:
        self.ib.reqMarketDataType(data_type)

    def open_trades(self) -> List[Trade]:
        return self.ib.openTrades()

    def place_order(self, contract: Contract, order: Order) -> Trade:
        return self.ib.placeOrder(contract, order)

    def cancel_order(self, order: Order) -> None:
        self.ib.cancelOrder(order)

    async def get_chains_for_contract(self, contract: Contract) -> List[OptionChain]:
        return await self.ib.reqSecDefOptParamsAsync(
            contract.symbol, "", contract.secType, contract.conId
        )

    async def qualify_contracts(self, *contracts: Contract) -> List[Contract]:
        return await self.ib.qualifyContractsAsync(*contracts)

    async def get_ticker_for_stock(
        self,
        symbol: str,
        primary_exchange: str,
        order_exchange: Optional[str] = None,
        generic_tick_list: str = "",
        required_fields: List[TickerField] = [TickerField.MARKET_PRICE],
        optional_fields: List[TickerField] = [TickerField.MIDPOINT],
    ) -> Ticker:
        stock = Stock(
            symbol,
            order_exchange or self.default_order_exchange,
            currency="USD",
            primaryExchange=primary_exchange,
        )
        return await self.get_ticker_for_contract(
            stock, generic_tick_list, required_fields, optional_fields
        )

    async def get_tickers_for_contracts(
        self,
        underlying_symbol: str,
        contracts: List[Contract],
        generic_tick_list: str = "",
        required_fields: List[TickerField] = [TickerField.MARKET_PRICE],
        optional_fields: List[TickerField] = [TickerField.MIDPOINT],
    ) -> List[Ticker]:
        async def get_ticker_task(contract: Contract) -> Ticker:
            return await self.get_ticker_for_contract(
                contract, generic_tick_list, required_fields, optional_fields
            )

        tasks = [get_ticker_task(contract) for contract in contracts]
        tickers = await log.track_async(
            tasks,
            description=f"{underlying_symbol}: Gathering tickers, waiting for required & optional fields...",
        )
        return tickers

    async def get_ticker_for_contract(
        self,
        contract: Contract,
        generic_tick_list: str = "",
        required_fields: List[TickerField] = [TickerField.MARKET_PRICE],
        optional_fields: List[TickerField] = [TickerField.MIDPOINT],
    ) -> Ticker:
        required_handlers = [
            self.__ticker_field_handler__(field) for field in required_fields
        ]
        optional_handlers = [
            self.__ticker_field_handler__(field) for field in optional_fields
        ]

        async def ticker_handler(ticker: Ticker) -> None:
            required_tasks = [handler(ticker) for handler in required_handlers]
            optional_tasks = [handler(ticker) for handler in optional_handlers]
            required_results, optional_results = await asyncio.gather(
                asyncio.gather(*required_tasks), asyncio.gather(*optional_tasks)
            )
            if not all(required_results):
                raise RequiredFieldValidationError(
                    "Not all required fields were processed successfully"
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
            lambda t: not (
                t.modelGreeks is None
                or t.modelGreeks.delta is None
                or util.isNan(t.modelGreeks.delta)
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
            log.warning(f"{trade.contract.symbol}: Order cancelled")
        else:
            log.info(
                f"{trade.contract.symbol}: Order updated with status={trade.orderStatus.status}"
            )

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
        await self.ib.qualifyContractsAsync(contract)
        ticker = self.ib.reqMktData(contract, genericTickList=generic_tick_list)
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

        ticker.updateEvent += onTicker  # type: ignore
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            ticker.updateEvent -= onTicker

    async def wait_for_submitting_orders(
        self, trades: List[Trade], timetout: int = 60
    ) -> None:
        tasks = [
            self.__trade_wait_for_condition__(
                trade,
                lambda trade: trade.orderStatus.status
                not in ["PendingSubmit", "PreSubmitted"],
                timetout,
            )
            for trade in trades
        ]
        await log.track_async(tasks, "Waiting for orders to be submitted...")

    async def wait_for_orders_complete(
        self, trades: List[Trade], timetout: int = 60
    ) -> None:
        tasks = [
            self.__trade_wait_for_condition__(
                trade,
                lambda trade: trade.isDone(),
                timetout,
            )
            for trade in trades
        ]
        await log.track_async(tasks, description="Waiting for orders to complete...")

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
        except asyncio.TimeoutError:
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
