from __future__ import annotations

import math
from typing import Callable, List, Optional, Tuple

from ib_async import TagValue, Ticker, util
from ib_async.contract import Contract, Option
from ib_async.order import LimitOrder

from thetagang import log
from thetagang.config import Config
from thetagang.db import DataStore
from thetagang.fmt import dfmt
from thetagang.ibkr import IBKR, TickerField
from thetagang.options import option_dte
from thetagang.orders import Orders
from thetagang.util import midpoint_or_market_price


class NoValidContractsError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)


class OrderOperations:
    def __init__(
        self,
        *,
        config: Config,
        account_number: str,
        orders: Orders,
        data_store: Optional[DataStore],
    ) -> None:
        self.config = config
        self.account_number = account_number
        self.orders = orders
        self.data_store = data_store

    def get_algo_strategy(self) -> str:
        return self.config.runtime.orders.algo.strategy

    def algo_params_from(self, params: List[List[str]]) -> List[TagValue]:
        return [TagValue(p[0], p[1]) for p in params]

    def get_algo_params(self) -> List[TagValue]:
        return self.algo_params_from(self.config.runtime.orders.algo.params)

    def get_order_exchange(self) -> str:
        return self.config.runtime.orders.exchange

    def round_vix_price(self, price: float) -> float:
        if price >= 3.0:
            return round(price * 20) / 20
        return round(price * 100) / 100

    def create_limit_order(
        self,
        *,
        action: str,
        quantity: float,
        limit_price: float,
        algo_strategy: str | None = None,
        algo_params: List[TagValue] | None = None,
        use_default_algo: bool = True,
        tif: str = "DAY",
        order_ref: str | None = None,
        transmit: bool = True,
        order_id: int | None = None,
    ) -> LimitOrder:
        kwargs = {
            "tif": tif,
            "account": self.account_number,
            "transmit": transmit,
        }
        if algo_strategy is not None:
            kwargs["algoStrategy"] = algo_strategy
        elif use_default_algo:
            kwargs["algoStrategy"] = self.get_algo_strategy()
        if algo_params is not None:
            kwargs["algoParams"] = algo_params
        elif use_default_algo:
            kwargs["algoParams"] = self.get_algo_params()
        if order_ref is not None:
            kwargs["orderRef"] = order_ref
        if order_id is not None:
            kwargs["orderId"] = order_id
        return LimitOrder(action, quantity, limit_price, **kwargs)

    def enqueue_order(self, contract: Optional[Contract], order: LimitOrder) -> None:
        if not contract:
            return
        intent_id = None
        if self.data_store:
            intent_id = self.data_store.record_order_intent(contract, order)
        self.orders.add_order(contract, order, intent_id)
        if self.data_store:
            self.data_store.record_event(
                "order_enqueued",
                {
                    "symbol": getattr(contract, "symbol", None),
                    "sec_type": getattr(contract, "secType", None),
                    "con_id": getattr(contract, "conId", None),
                    "exchange": getattr(contract, "exchange", None),
                    "currency": getattr(contract, "currency", None),
                    "action": getattr(order, "action", None),
                    "quantity": getattr(order, "totalQuantity", None),
                    "limit_price": getattr(order, "lmtPrice", None),
                    "order_type": getattr(order, "orderType", None),
                    "order_ref": getattr(order, "orderRef", None),
                    "intent_id": intent_id,
                },
                symbol=getattr(contract, "symbol", None),
            )


class OptionChainScanner:
    def __init__(
        self, *, config: Config, ibkr: IBKR, order_ops: OrderOperations
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.order_ops = order_ops

    async def find_eligible_contracts(
        self,
        underlying: Contract,
        right: str,
        strike_limit: Optional[float],
        minimum_price: Callable[[], float],
        exclude_expirations_before: Optional[str] = None,
        exclude_exp_strike: Optional[Tuple[float, str]] = None,
        fallback_minimum_price: Optional[Callable[[], float]] = None,
        target_dte: Optional[int] = None,
        target_delta: Optional[float] = None,
    ) -> Ticker:
        contract_target_dte: int = (
            target_dte if target_dte else self.config.get_target_dte(underlying.symbol)
        )
        contract_target_delta: float = (
            target_delta
            if target_delta
            else self.config.get_target_delta(underlying.symbol, right)
        )
        contract_max_dte = self.config.get_max_dte_for(underlying.symbol)

        log.notice(
            f"{underlying.symbol}: Searching option chain for "
            f"right={right} strike_limit={strike_limit} minimum_price={dfmt(minimum_price(), 3)} "
            f"fallback_minimum_price={dfmt(fallback_minimum_price() if fallback_minimum_price else 0, 3)} "
            f"contract_target_dte={contract_target_dte} contract_max_dte={contract_max_dte} "
            f"contract_target_delta={contract_target_delta}, "
            "this can take a while...",
        )

        underlying_ticker = await self.ibkr.get_ticker_for_contract(underlying)
        underlying_price = midpoint_or_market_price(underlying_ticker)
        chains = await self.ibkr.get_chains_for_contract(underlying)
        chain = next(
            c
            for c in chains
            if c.exchange == underlying.exchange and c.tradingClass == underlying.symbol
        )

        def valid_strike(strike: float) -> bool:
            if right.startswith("P") and strike_limit:
                return strike <= strike_limit
            elif right.startswith("P"):
                return strike <= underlying_price + 0.05 * underlying_price
            elif right.startswith("C") and strike_limit:
                return strike >= strike_limit
            elif right.startswith("C"):
                return strike >= underlying_price - 0.05 * underlying_price
            return False

        chain_expirations = self.config.runtime.option_chains.expirations
        min_dte = (
            option_dte(exclude_expirations_before) if exclude_expirations_before else 0
        )
        strikes = sorted(strike for strike in chain.strikes if valid_strike(strike))
        expirations = sorted(
            exp
            for exp in chain.expirations
            if option_dte(exp) >= contract_target_dte
            and option_dte(exp) >= min_dte
            and (not contract_max_dte or option_dte(exp) <= contract_max_dte)
        )[:chain_expirations]
        if len(expirations) < 1:
            raise NoValidContractsError(
                f"No valid contract expirations found for {underlying.symbol}. Continuing anyway..."
            )

        def nearest_strikes(strikes: List[float]) -> List[float]:
            chain_strikes = self.config.runtime.option_chains.strikes
            if right.startswith("P"):
                return strikes[-chain_strikes:]
            return strikes[:chain_strikes]

        strikes = nearest_strikes(strikes)
        if len(strikes) < 1:
            raise NoValidContractsError(
                f"No valid contract strikes found for {underlying.symbol}. Continuing anyway..."
            )
        log.info(
            f"{underlying.symbol}: Scanning between strikes {strikes[0]} and {strikes[-1]},"
            f" from expirations {expirations[0]} to {expirations[-1]}"
        )

        contracts = [
            Option(
                underlying.symbol,
                expiration,
                strike,
                right,
                self.order_ops.get_order_exchange(),
            )
            for expiration in expirations
            for strike in strikes
        ]
        contracts = await self.ibkr.qualify_contracts(*contracts)
        contracts = [c for c in contracts if c is not None]

        if exclude_exp_strike:
            contracts = [
                c
                for c in contracts
                if (
                    c.lastTradeDateOrContractMonth != exclude_exp_strike[1]
                    or c.strike != exclude_exp_strike[0]
                )
            ]

        tickers = await self.ibkr.get_tickers_for_contracts(
            underlying.symbol,
            contracts,
            generic_tick_list="101",
            required_fields=[],
            optional_fields=[
                TickerField.MARKET_PRICE,
                TickerField.GREEKS,
                TickerField.OPEN_INTEREST,
                TickerField.MIDPOINT,
            ],
        )

        def open_interest_is_valid(ticker: Ticker, minimum_open_interest: int) -> bool:
            if right.startswith("P"):
                return ticker.putOpenInterest >= minimum_open_interest
            if right.startswith("C"):
                return ticker.callOpenInterest >= minimum_open_interest
            return False

        def delta_is_valid(ticker: Ticker) -> bool:
            model_greeks = ticker.modelGreeks
            delta = model_greeks.delta if model_greeks is not None else None
            return (
                delta is not None
                and not util.isNan(delta)
                and abs(delta) <= contract_target_delta
            )

        def price_is_valid(ticker: Ticker) -> bool:
            def cost_doesnt_exceed_market_price(ticker: Ticker) -> bool:
                return (
                    right.startswith("C")
                    or isinstance(ticker.contract, Option)
                    and ticker.contract.strike
                    <= midpoint_or_market_price(ticker) + underlying_price
                )

            return midpoint_or_market_price(
                ticker
            ) > minimum_price() and cost_doesnt_exceed_market_price(ticker)

        tickers = [
            ticker
            for ticker in log.track(
                tickers,
                description=f"{underlying.symbol}: Filtering invalid prices...",
                total=len(tickers),
            )
            if price_is_valid(ticker)
        ]

        new_tickers = []
        delta_reject_tickers = []
        for ticker in log.track(
            tickers,
            description=f"{underlying.symbol}: Filtering invalid deltas...",
            total=len(tickers),
        ):
            if delta_is_valid(ticker):
                new_tickers.append(ticker)
            else:
                delta_reject_tickers.append(ticker)
        tickers = new_tickers

        def filter_remaining_tickers(
            tickers: List[Ticker], delta_ord_desc: bool
        ) -> List[Ticker]:
            minimum_open_interest = (
                self.config.strategies.wheel.defaults.target.minimum_open_interest
            )
            if minimum_open_interest > 0:
                tickers = [
                    ticker
                    for ticker in log.track(
                        tickers,
                        description=f"{underlying.symbol}: Filtering by open interest with delta_ord_desc={delta_ord_desc}...",
                        total=len(tickers),
                    )
                    if open_interest_is_valid(ticker, minimum_open_interest)
                ]

            return sorted(
                sorted(
                    tickers,
                    key=lambda t: (
                        abs(t.modelGreeks.delta)
                        if t.modelGreeks and t.modelGreeks.delta
                        else 0
                    ),
                    reverse=delta_ord_desc,
                ),
                key=lambda t: (
                    option_dte(t.contract.lastTradeDateOrContractMonth)
                    if t.contract
                    else 0
                ),
            )

        tickers = filter_remaining_tickers(list(tickers), True)
        chosen = None
        if len(tickers) == 0:
            if not math.isclose(minimum_price(), 0.0):
                tickers = filter_remaining_tickers(list(delta_reject_tickers), False)
            if len(tickers) < 1:
                raise NoValidContractsError(
                    f"No valid contracts found for {underlying.symbol}. Continuing anyway..."
                )
        elif fallback_minimum_price is not None:
            for ticker in tickers:
                if midpoint_or_market_price(ticker) > fallback_minimum_price():
                    chosen = ticker
                    break
            if chosen is None:
                tickers = sorted(tickers, key=midpoint_or_market_price, reverse=True)

        if chosen is None:
            chosen = tickers[0]
        if not chosen or not chosen.contract:
            raise RuntimeError(
                f"{underlying.symbol}: Something went wrong, the_chosen_ticker={chosen}"
            )
        log.notice(
            f"{underlying.symbol}: Found suitable contract at "
            f"strike={chosen.contract.strike} "
            f"dte={option_dte(chosen.contract.lastTradeDateOrContractMonth)} "
            f"price={dfmt(midpoint_or_market_price(chosen), 3)}"
        )
        return chosen
