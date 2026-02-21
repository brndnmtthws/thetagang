from __future__ import annotations

import math
from typing import Dict, List, Optional

from ib_async import AccountValue, PortfolioItem, Ticker, util
from ib_async.contract import Contract, Index, Option, Stock

from thetagang import log
from thetagang.config import Config
from thetagang.ibkr import IBKR
from thetagang.orders import Orders
from thetagang.trading_operations import (
    NoValidContractsError,
    OptionChainScanner,
    OrderOperations,
)
from thetagang.util import get_lower_price, net_option_positions


class PostStrategyEngine:
    def __init__(
        self,
        *,
        config: Config,
        ibkr: IBKR,
        order_ops: OrderOperations,
        option_scanner: OptionChainScanner,
        orders: Orders,
        qualified_contracts: Dict[int, Contract],
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.order_ops = order_ops
        self.option_scanner = option_scanner
        self.orders = orders
        self.qualified_contracts = qualified_contracts

    def calc_pending_cash_balance(self) -> float:
        def get_multiplier(contract: Contract) -> float:
            if contract.secType == "BAG":
                return float(
                    self.qualified_contracts[contract.comboLegs[0].conId].multiplier
                )
            if contract.secType == "STK":
                return 1.0
            return float(contract.multiplier or 100)

        return sum(
            [
                float(order.lmtPrice or 0)
                * order.totalQuantity
                * get_multiplier(contract)
                for (contract, order, _intent_id) in self.orders.records()
                if order.action == "SELL"
            ]
        ) - sum(
            [
                float(order.lmtPrice or 0)
                * order.totalQuantity
                * get_multiplier(contract)
                for (contract, order, _intent_id) in self.orders.records()
                if order.action == "BUY"
            ]
        )

    async def do_vix_hedging(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> None:
        log.notice("VIX: Checking on our VIX call hedge...")

        async def vix_calls_should_be_closed() -> tuple[
            bool, Optional[Ticker], Optional[float]
        ]:
            if self.config.strategies.vix_call_hedge.close_hedges_when_vix_exceeds:
                vix_contract = Index("VIX", "CBOE", "USD")
                vix_ticker = await self.ibkr.get_ticker_for_contract(vix_contract)
                threshold = (
                    self.config.strategies.vix_call_hedge.close_hedges_when_vix_exceeds
                )
                return (
                    bool(vix_ticker.marketPrice() > threshold),
                    vix_ticker,
                    threshold,
                )
            return (False, None, None)

        if not self.config.strategies.vix_call_hedge.enabled:
            log.warning("🛑 VIX call hedging not enabled, skipping...")
            return

        ignore_dte = self.config.strategies.vix_call_hedge.ignore_dte
        net_vix_call_count = net_option_positions(
            "VIX", portfolio_positions, "C", ignore_dte=ignore_dte
        )
        if net_vix_call_count > 0:
            (
                close_vix_calls,
                vix_ticker,
                threshold,
            ) = await vix_calls_should_be_closed()
            if close_vix_calls and vix_ticker and threshold:
                for position in portfolio_positions.get("VIX", []):
                    if (
                        position.contract.right.startswith("C")
                        and position.position < 0
                    ):
                        continue
                    position.contract.exchange = self.order_ops.get_order_exchange()
                    sell_ticker = await self.ibkr.get_ticker_for_contract(
                        position.contract
                    )
                    price = self.order_ops.round_vix_price(
                        round(get_lower_price(sell_ticker), 2)
                    )
                    qty = abs(position.position)
                    order = self.order_ops.create_limit_order(
                        action="SELL",
                        quantity=qty,
                        limit_price=price,
                        transmit=True,
                    )
                    self.order_ops.enqueue_order(sell_ticker.contract, order)
            return

        (close_vix_calls, _vix_ticker, _threshold) = await vix_calls_should_be_closed()
        if close_vix_calls:
            return
        try:
            vixmo_contract = Index("VIXMO", "CBOE", "USD")
            vixmo_ticker = await self.ibkr.get_ticker_for_contract(vixmo_contract)
            weight = 0.0
            for allocation in self.config.strategies.vix_call_hedge.allocation:
                if (
                    allocation.lower_bound
                    and allocation.upper_bound
                    and allocation.lower_bound
                    <= vixmo_ticker.marketPrice()
                    < allocation.upper_bound
                ):
                    weight = allocation.weight
                    break
                elif (
                    allocation.lower_bound
                    and allocation.lower_bound <= vixmo_ticker.marketPrice()
                ):
                    weight = allocation.weight
                    break
                elif (
                    allocation.upper_bound
                    and vixmo_ticker.marketPrice() < allocation.upper_bound
                ):
                    weight = allocation.weight
                    break
            allocation_amount = float(account_summary["NetLiquidation"].value) * weight
            if weight <= 0:
                return
            buy_ticker = await self.option_scanner.find_eligible_contracts(
                Index("VIX", "CBOE", "USD"),
                "C",
                0,
                target_delta=self.config.strategies.vix_call_hedge.delta,
                target_dte=self.config.strategies.vix_call_hedge.target_dte,
                minimum_price=lambda: self.config.runtime.orders.minimum_credit,
            )
            if not isinstance(buy_ticker.contract, Option):
                raise RuntimeError(f"Something went wrong, buy_ticker={buy_ticker}")
            price = self.order_ops.round_vix_price(
                round(get_lower_price(buy_ticker), 2)
            )
            qty = math.floor(
                allocation_amount / price / float(buy_ticker.contract.multiplier)
            )
            order = self.order_ops.create_limit_order(
                action="BUY",
                quantity=qty,
                limit_price=price,
                transmit=True,
            )
            self.order_ops.enqueue_order(buy_ticker.contract, order)
        except (RuntimeError, NoValidContractsError):
            log.error("VIX: Error occurred when VIX call hedging. Continuing anyway...")

    async def do_cashman(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> None:
        log.notice("Cash management...")
        if not self.config.strategies.cash_management.enabled:
            log.warning("🛑 Cash management not enabled, skipping")
            return

        target_cash_balance = self.config.strategies.cash_management.target_cash_balance
        buy_threshold = self.config.strategies.cash_management.buy_threshold
        sell_threshold = self.config.strategies.cash_management.sell_threshold
        cash_balance = math.floor(float(account_summary["TotalCashValue"].value))
        pending_balance = self.calc_pending_cash_balance()
        try:
            if not (
                cash_balance + pending_balance > target_cash_balance + buy_threshold
                or cash_balance + pending_balance < target_cash_balance - sell_threshold
            ):
                return

            symbol = self.config.strategies.cash_management.cash_fund
            primary_exchange = self.config.strategies.cash_management.primary_exchange
            order_exchange = self.config.strategies.cash_management.orders.exchange
            ticker = await self.ibkr.get_ticker_for_stock(
                symbol, primary_exchange, order_exchange
            )

            algo = (
                self.config.strategies.cash_management.orders.algo
                if self.config.strategies.cash_management.orders
                else self.config.runtime.orders.algo
            )
            amount = cash_balance + pending_balance - target_cash_balance
            price = ticker.ask if amount > 0 else ticker.bid
            qty = amount // price
            if util.isNan(qty):
                raise RuntimeError("ERROR: qty is NaN")

            if qty < 0:
                qty -= 1
                if symbol not in portfolio_positions:
                    return
                positions = [
                    p.position
                    for p in portfolio_positions[symbol]
                    if isinstance(p.contract, Stock)
                ]
                position = positions[0] if len(positions) > 0 else 0
                qty = min([max([-math.floor(position), qty]), 0])
                if qty == 0:
                    return

            order = self.order_ops.create_limit_order(
                action="BUY" if qty > 0 else "SELL",
                quantity=abs(qty),
                limit_price=round(price, 2),
                algo_strategy=algo.strategy,
                algo_params=self.order_ops.algo_params_from(algo.params),
                transmit=True,
            )
            self.order_ops.enqueue_order(ticker.contract, order)
        except RuntimeError:
            log.error("Error occurred when cash hedging. Continuing anyway...")
