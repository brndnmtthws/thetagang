from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional

from ib_async import AccountValue, PortfolioItem, Ticker, util
from ib_async.contract import Contract, Index, Option, Stock

from thetagang import log
from thetagang.config import Config
from thetagang.fmt import dfmt
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

    def reserved_cash_for_post_management(self) -> float:
        if self._get_reserved_cash_for_post_management is None:
            return 0.0
        return max(0.0, self._get_reserved_cash_for_post_management())

    def calc_pending_cash_balance(self) -> float:
        from thetagang.batch_risk import pending_debit

        # Only filled proceeds can fund a sweep. Pending credits, including
        # negative-price combo BUYs, do not replenish spendable cash.
        return -sum(
            pending_debit(contract, order, self.qualified_contracts)
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
            self.order_ops.record_decision(
                "VIX", "vix_hedge", "skipped", "VIX 对冲未启用"
            )
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
            self.order_ops.record_decision(
                "VIX",
                "vix_hedge",
                "eligible" if close_vix_calls else "skipped",
                "已有 VIX Call，达到对冲平仓门槛"
                if close_vix_calls
                else "已有有效 VIX Call 持仓，本次不新增对冲",
            )
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
            self.order_ops.record_decision(
                "VIX", "vix_hedge", "skipped", "VIX 已高于配置的平仓阈值，跳过新增对冲"
            )
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
            self.order_ops.record_decision(
                "VIX",
                "vix_hedge",
                "eligible" if weight > 0 else "skipped",
                "按当前 VIXMO 与配置计算对冲预算"
                if weight > 0
                else "当前配置匹配到零对冲预算",
                vixmo=vixmo_ticker.marketPrice(),
                weight=weight,
                allocation_amount=allocation_amount,
            )
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
            self.order_ops.record_decision(
                "VIX",
                "vix_hedge",
                "planned",
                "对冲预算除以每张权利金，向下取整后加入买入计划",
                quantity=qty,
                limit_price=price,
                strike=buy_ticker.contract.strike,
                allocation_amount=allocation_amount,
            )
        except (RuntimeError, NoValidContractsError):
            self.order_ops.record_decision(
                "VIX", "vix_hedge", "error", "对冲行情获取或合约筛选失败，跳过本次对冲"
            )
            log.error("VIX: Error occurred when VIX call hedging. Continuing anyway...")

    async def do_cashman(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> None:
        log.notice("Cash management...")
        if not self.config.strategies.cash_management.enabled:
            self.order_ops.record_decision(
                "", "cash_management", "skipped", "现金管理未启用"
            )
            log.warning("🛑 Cash management not enabled, skipping")
            return

        target_cash_balance = self.config.strategies.cash_management.target_cash_balance
        buy_threshold = self.config.strategies.cash_management.buy_threshold
        sell_threshold = self.config.strategies.cash_management.sell_threshold
        cash_balance = math.floor(float(account_summary["TotalCashValue"].value))
        pending_balance = self.calc_pending_cash_balance()
        effective_cash_balance = cash_balance + pending_balance
        reserved_cash = self.reserved_cash_for_post_management()
        sweepable_cash_balance = effective_cash_balance - reserved_cash
        if reserved_cash > 0:
            log.notice(
                "Cash management: reserving "
                f"{dfmt(reserved_cash)} for regime flow deployment."
            )
        try:
            amount = 0.0
            if sweepable_cash_balance > target_cash_balance + buy_threshold:
                amount = sweepable_cash_balance - target_cash_balance
            elif effective_cash_balance < target_cash_balance - sell_threshold:
                amount = effective_cash_balance - target_cash_balance
            else:
                self.order_ops.record_decision(
                    "",
                    "cash_management",
                    "skipped",
                    "现金余额处于配置允许的范围内",
                    cash_balance=cash_balance,
                    pending_balance=pending_balance,
                    effective_cash_balance=effective_cash_balance,
                )
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
            price = ticker.ask if amount > 0 else ticker.bid
            qty = amount // price
            if util.isNan(qty):
                raise RuntimeError("ERROR: qty is NaN")

            if qty < 0:
                qty -= 1
                if symbol not in portfolio_positions:
                    self.order_ops.record_decision(
                        symbol,
                        "cash_management",
                        "skipped",
                        "现金不足，但没有可出售的现金基金持仓",
                    )
                    return
                positions = [
                    p.position
                    for p in portfolio_positions[symbol]
                    if isinstance(p.contract, Stock)
                ]
                position = positions[0] if len(positions) > 0 else 0
                qty = min([max([-math.floor(position), qty]), 0])
                if qty == 0:
                    self.order_ops.record_decision(
                        symbol, "cash_management", "skipped", "可出售的现金基金股数不足"
                    )
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
            self.order_ops.record_decision(
                symbol,
                "cash_management",
                "planned",
                "现金超过目标，买入现金基金"
                if qty > 0
                else "现金低于目标，卖出现金基金补足；未成交卖单收入不计入可用现金",
                cash_balance=cash_balance,
                pending_balance=pending_balance,
                effective_cash_balance=effective_cash_balance,
                quantity=abs(qty),
                limit_price=round(price, 2),
            )
        except RuntimeError:
            self.order_ops.record_decision(
                "", "cash_management", "error", "现金管理行情获取或订单计算失败"
            )
            log.error("Error occurred when cash hedging. Continuing anyway...")
