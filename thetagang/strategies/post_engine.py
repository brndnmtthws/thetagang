from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional

from ib_async import AccountValue, PortfolioItem, Ticker, util
from ib_async.contract import Contract, Index, Option, Stock

from thetagang import log
from thetagang.config import Config
from thetagang.db import DataStore
from thetagang.fmt import dfmt
from thetagang.ibkr import IBKR
from thetagang.orders import Orders, pending_order_cash
from thetagang.trading_operations import (
    NoValidContractsError,
    OptionChainScanner,
    OrderOperations,
)
from thetagang.util import (
    get_lower_price,
    net_option_positions,
    portfolio_positions_to_dict,
)

from .tail_hedge_engine import TailHedgeEngine


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
        data_store: Optional[DataStore] = None,
        get_reserved_cash_for_post_management: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.order_ops = order_ops
        self.option_scanner = option_scanner
        self.orders = orders
        self.qualified_contracts = qualified_contracts
        self._get_reserved_cash_for_post_management = (
            get_reserved_cash_for_post_management
        )
        self.tail_hedge_engine = TailHedgeEngine(
            config=config,
            ibkr=ibkr,
            order_ops=order_ops,
            data_store=data_store,
        )

    def reserved_cash_for_post_management(self) -> float:
        if self._get_reserved_cash_for_post_management is None:
            return 0.0
        return max(0.0, self._get_reserved_cash_for_post_management())

    def pending_cash_components(self) -> tuple[float, float]:
        pending = pending_order_cash(
            self.ibkr.open_trades(),
            self.orders.records(),
            account=self.config.runtime.account.number,
            qualified_contracts=self.qualified_contracts,
            estimated_fee_per_contract=float(
                getattr(
                    self.config.runtime.orders,
                    "estimated_fee_per_contract",
                    0.0,
                )
            ),
        )
        if pending.ambiguous:
            raise RuntimeError("Pending order cash cannot be priced safely")
        return pending.debit, pending.credit

    def calc_pending_cash_balance(self) -> float:
        pending_debit, pending_credit = self.pending_cash_components()
        return pending_credit - pending_debit

    def _cash_fund_order_pending(self, symbol: str) -> bool:
        account = self.config.runtime.account.number

        def matches(contract: object, order: object) -> bool:
            return (
                isinstance(contract, Stock)
                and contract.symbol == symbol
                and getattr(order, "account", None) == account
                and str(getattr(order, "action", "")).upper() in {"BUY", "SELL"}
            )

        for trade in self.ibkr.open_trades():
            is_done = getattr(trade, "isDone", None)
            if not (callable(is_done) and is_done()) and matches(
                getattr(trade, "contract", None),
                getattr(trade, "order", None),
            ):
                return True
        return any(
            matches(contract, order)
            for contract, order, _intent_id in self.orders.records()
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

    async def do_tail_hedging(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> None:
        await self.tail_hedge_engine.manage(
            portfolio_positions,
            net_liquidation=float(account_summary["NetLiquidation"].value),
        )

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
        symbol = self.config.strategies.cash_management.cash_fund
        if self._cash_fund_order_pending(symbol):
            return

        def amount_to_manage(summary: Dict[str, AccountValue]) -> float:
            cash_balance = math.floor(float(summary["TotalCashValue"].value))
            (
                pending_debit,
                pending_credit,
            ) = self.pending_cash_components()
            cash_after_pending_debits = cash_balance - pending_debit
            sweepable_cash_balance = (
                cash_after_pending_debits - self.reserved_cash_for_post_management()
            )
            # Pending credits can prevent duplicate liquidation, but cannot fund
            # a new cash-fund purchase until they settle.
            if sweepable_cash_balance > target_cash_balance + buy_threshold:
                return sweepable_cash_balance - target_cash_balance
            projected_cash_balance = cash_after_pending_debits + pending_credit
            if projected_cash_balance < target_cash_balance - sell_threshold:
                return projected_cash_balance - target_cash_balance
            return 0.0

        reserved_cash = self.reserved_cash_for_post_management()
        if reserved_cash > 0:
            log.notice(
                "Cash management: reserving "
                f"{dfmt(reserved_cash)} for regime rebalancing."
            )
        try:
            amount = amount_to_manage(account_summary)
            if amount == 0:
                return

            primary_exchange = self.config.strategies.cash_management.primary_exchange
            order_exchange = self.config.strategies.cash_management.orders.exchange
            ticker = await self.ibkr.get_ticker_for_stock(
                symbol, primary_exchange, order_exchange
            )

            # Re-materialize ib_async's fill-current caches after the quote
            # await, then size and enqueue without another await.
            account_number = self.config.runtime.account.number
            live_cash = self.ibkr.cached_account_value(account_number, "TotalCashValue")
            account_summary = dict(account_summary)
            account_summary["TotalCashValue"] = AccountValue(
                account_number, "TotalCashValue", str(live_cash), "BASE", ""
            )
            portfolio_positions = portfolio_positions_to_dict(
                self.ibkr.portfolio(account=account_number)
            )
            if self._cash_fund_order_pending(symbol):
                return
            amount = amount_to_manage(account_summary)
            if amount == 0:
                return

            algo = (
                self.config.strategies.cash_management.orders.algo
                if self.config.strategies.cash_management.orders
                else self.config.runtime.orders.algo
            )
            price = ticker.ask if amount > 0 else ticker.bid
            qty = amount // price
            if util.isNan(qty) or not math.isfinite(float(qty)):
                raise RuntimeError("ERROR: qty is NaN")
            if qty == 0:
                return

            if qty < 0:
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
