from __future__ import annotations

import math
from typing import Any, Coroutine, Dict, List, Protocol, Tuple

from ib_async import AccountValue, PortfolioItem
from ib_async.contract import Stock
from rich.table import Table

from thetagang import log
from thetagang.config import Config
from thetagang.fmt import ifmt
from thetagang.ibkr import IBKR, TickerField
from thetagang.strategies.regime_engine import RegimeRebalanceEngine
from thetagang.strategies.runtime_services import resolve_symbol_configs
from thetagang.trading_operations import OrderOperations


class EquityRuntimeServices(Protocol):
    def get_primary_exchange(self, symbol: str) -> str: ...

    def get_buying_power(self, account_summary: Dict[str, AccountValue]) -> int: ...

    def midpoint_or_market_price(self, ticker: Any) -> float: ...


class EquityRebalanceEngine:
    def __init__(
        self,
        *,
        config: Config,
        ibkr: IBKR,
        order_ops: OrderOperations,
        services: EquityRuntimeServices,
        regime_engine: RegimeRebalanceEngine,
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.order_ops = order_ops
        self.services = services
        self.regime_engine = regime_engine
        self.regime_rebalance_order_ref_prefix = "tg:regime-rebalance"

    def _regime_rebalance_symbols(self) -> set[str]:
        regime_rebalance = getattr(self.config, "regime_rebalance", None)
        if not regime_rebalance or not getattr(regime_rebalance, "enabled", False):
            return set()
        symbols = getattr(regime_rebalance, "symbols", [])
        if not isinstance(symbols, (list, tuple, set)):
            return set()
        return set(symbols)

    def get_primary_exchange(self, symbol: str) -> str:
        return self.services.get_primary_exchange(symbol)

    def get_buying_power(self, account_summary: Dict[str, AccountValue]) -> int:
        return self.services.get_buying_power(account_summary)

    def _midpoint_or_market_price(self, ticker: Any) -> float:
        return float(self.services.midpoint_or_market_price(ticker))

    @staticmethod
    def _as_int_or_none(value: Any) -> int | None:
        return value if isinstance(value, int) else None

    @staticmethod
    def _as_float_or_none(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    async def execute_regime_rebalance_orders(
        self, orders: List[Tuple[str, str, int]]
    ) -> None:
        for symbol, primary_exchange, quantity in orders:
            try:
                action = "BUY" if quantity > 0 else "SELL"
                stock_contract = Stock(
                    symbol,
                    self.order_ops.get_order_exchange(),
                    currency="USD",
                    primaryExchange=primary_exchange,
                )
                ticker = await self.ibkr.get_ticker_for_contract(
                    stock_contract,
                    required_fields=[],
                    optional_fields=[TickerField.MIDPOINT, TickerField.MARKET_PRICE],
                )
                limit_price = round(self._midpoint_or_market_price(ticker), 2)
                order = self.order_ops.create_limit_order(
                    action=action,
                    quantity=abs(quantity),
                    limit_price=limit_price,
                    order_ref=f"{self.regime_rebalance_order_ref_prefix}:{symbol}",
                    transmit=True,
                )
                log.notice(
                    f"Regime rebalancing: {action.lower()} {abs(quantity)} shares of {symbol} @ ${limit_price}"
                )
                self.order_ops.enqueue_order(stock_contract, order)
            except Exception as e:
                log.error(
                    f"{symbol}: Failed to execute regime rebalance order. Error: {e}"
                )
                continue

    async def check_regime_rebalance_positions(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Tuple[Table, List[Tuple[str, str, int]]]:
        return await self.regime_engine.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )

    async def check_buy_only_positions(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Tuple[Table, List[Tuple[str, str, int]]]:
        stock_positions = [
            position
            for symbol in portfolio_positions
            for position in portfolio_positions[symbol]
            if isinstance(position.contract, Stock)
        ]
        total_buying_power = self.get_buying_power(account_summary)
        stock_symbols: Dict[str, PortfolioItem] = {
            stock.contract.symbol: stock for stock in stock_positions
        }

        buy_actions_table = Table(title="Buy-only rebalancing summary")
        buy_actions_table.add_column("Symbol")
        buy_actions_table.add_column("Current shares", justify="right")
        buy_actions_table.add_column("Target shares", justify="right")
        buy_actions_table.add_column("Shares to buy", justify="right")
        buy_actions_table.add_column("Action")

        to_buy: List[Tuple[str, str, int]] = []
        regime_symbols = self._regime_rebalance_symbols()
        symbols = resolve_symbol_configs(self.config, context="buy-only rebalancing")
        buy_only_symbols = [
            symbol
            for symbol in symbols.keys()
            if self.config.is_buy_only_rebalancing(symbol)
            and symbol not in regime_symbols
        ]
        if not buy_only_symbols:
            return (buy_actions_table, to_buy)

        async def check_buy_position_task(symbol: str) -> None:
            ticker = await self.ibkr.get_ticker_for_stock(
                symbol, self.get_primary_exchange(symbol)
            )
            current_position = math.floor(
                stock_symbols[symbol].position if symbol in stock_symbols else 0
            )
            target_value = round(symbols[symbol].weight * total_buying_power, 2)
            market_price = ticker.marketPrice()
            if (
                not market_price
                or math.isnan(market_price)
                or math.isclose(market_price, 0)
            ):
                log.error(
                    f"Invalid market price for {symbol} (market_price={market_price}), skipping for now"
                )
                return
            target_shares = math.floor(target_value / market_price)
            shares_to_buy = target_shares - current_position

            rebalance_policy = self.config.wheel_rebalance_policy(symbol)
            symbol_config = symbols[symbol]
            min_shares = (
                self._as_int_or_none(rebalance_policy.min_threshold_shares)
                or self._as_int_or_none(symbol_config.buy_only_min_threshold_shares)
                or 1
            )
            min_amount = self._as_float_or_none(
                rebalance_policy.min_threshold_amount
            ) or self._as_float_or_none(symbol_config.buy_only_min_threshold_amount)
            min_percent = self._as_float_or_none(
                rebalance_policy.min_threshold_percent
            ) or self._as_float_or_none(symbol_config.buy_only_min_threshold_percent)
            min_percent_relative = self._as_float_or_none(
                rebalance_policy.min_threshold_percent_relative
            ) or self._as_float_or_none(
                symbol_config.buy_only_min_threshold_percent_relative
            )

            if min_percent is not None:
                net_liquidation_value = float(account_summary["NetLiquidation"].value)
                percent_min_amount = net_liquidation_value * min_percent
                min_amount = (
                    max(min_amount, percent_min_amount)
                    if min_amount is not None
                    else percent_min_amount
                )

            if (
                min_percent_relative is not None
                and target_value > 0
                and shares_to_buy > 0
            ):
                current_value = current_position * market_price
                relative_diff = (target_value - current_value) / target_value
                if relative_diff < min_percent_relative:
                    buy_actions_table.add_row(
                        symbol,
                        ifmt(current_position),
                        ifmt(target_shares),
                        ifmt(0),
                        f"[yellow]Below relative threshold {min_percent_relative:.1%} (diff: {relative_diff:.1%})",
                    )
                    return

            if shares_to_buy <= 0 and current_position == 0 and target_value > 0:
                if min_amount and min_amount < market_price:
                    shares_to_buy = 1 - current_position
                elif not min_amount and min_shares == 1:
                    shares_to_buy = 1 - current_position

            if shares_to_buy > 0:
                order_amount = shares_to_buy * market_price
                if min_amount and order_amount < min_amount:
                    if min_amount < market_price:
                        shares_to_buy = 1
                        order_amount = market_price
                    else:
                        buy_actions_table.add_row(
                            symbol,
                            ifmt(current_position),
                            ifmt(target_shares),
                            ifmt(shares_to_buy),
                            f"[yellow]Below min amount ${min_amount:.2f} (would be ${order_amount:.2f})",
                        )
                        return
                if shares_to_buy < min_shares:
                    buy_actions_table.add_row(
                        symbol,
                        ifmt(current_position),
                        ifmt(target_shares),
                        ifmt(shares_to_buy),
                        f"[yellow]Below min shares {min_shares}",
                    )
                    return

                cost = shares_to_buy * market_price
                available_buying_power = self.get_buying_power(account_summary)
                if cost > available_buying_power:
                    shares_to_buy = math.floor(available_buying_power / market_price)
                    order_amount = shares_to_buy * market_price
                    if min_amount and order_amount < min_amount:
                        if (
                            available_buying_power >= market_price
                            and min_amount < market_price
                        ):
                            shares_to_buy = 1
                        else:
                            buy_actions_table.add_row(
                                symbol,
                                ifmt(current_position),
                                ifmt(target_shares),
                                ifmt(0),
                                f"[yellow]Insufficient buying power to meet min amount ${min_amount:.2f}",
                            )
                            return
                    if shares_to_buy < min_shares:
                        buy_actions_table.add_row(
                            symbol,
                            ifmt(current_position),
                            ifmt(target_shares),
                            ifmt(0),
                            f"[yellow]Insufficient buying power to meet min shares {min_shares}",
                        )
                        return

                if shares_to_buy > 0:
                    buy_actions_table.add_row(
                        symbol,
                        ifmt(current_position),
                        ifmt(target_shares),
                        ifmt(shares_to_buy),
                        f"[green]Buy {shares_to_buy} shares",
                    )
                    to_buy.append(
                        (symbol, self.get_primary_exchange(symbol), shares_to_buy)
                    )
                else:
                    buy_actions_table.add_row(
                        symbol,
                        ifmt(current_position),
                        ifmt(target_shares),
                        ifmt(0),
                        "[yellow]Insufficient buying power",
                    )
            else:
                buy_actions_table.add_row(
                    symbol,
                    ifmt(current_position),
                    ifmt(target_shares),
                    ifmt(0),
                    "[cyan]At or above target",
                )

        tasks: List[Coroutine[Any, Any, None]] = [
            check_buy_position_task(symbol) for symbol in buy_only_symbols
        ]
        await log.track_async(tasks, description="Checking buy-only positions...")
        return (buy_actions_table, to_buy)

    async def execute_buy_orders(self, buy_orders: List[Tuple[str, str, int]]) -> None:
        for symbol, primary_exchange, quantity in buy_orders:
            try:
                stock_contract = Stock(
                    symbol,
                    self.order_ops.get_order_exchange(),
                    currency="USD",
                    primaryExchange=primary_exchange,
                )
                ticker = await self.ibkr.get_ticker_for_contract(
                    stock_contract,
                    required_fields=[],
                    optional_fields=[TickerField.MIDPOINT, TickerField.MARKET_PRICE],
                )
                limit_price = round(self._midpoint_or_market_price(ticker), 2)
                order = self.order_ops.create_limit_order(
                    action="BUY",
                    quantity=quantity,
                    limit_price=limit_price,
                    transmit=True,
                )
                log.notice(
                    f"Buy-only rebalancing: buying {quantity} shares of {symbol} @ ${limit_price}"
                )
                self.order_ops.enqueue_order(stock_contract, order)
            except Exception as e:
                log.error(
                    f"{symbol}: Failed to execute buy order for {quantity} shares. Error: {e}"
                )
                continue

    async def check_sell_only_positions(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Tuple[Table, List[Tuple[str, str, int]]]:
        stock_positions = [
            position
            for symbol in portfolio_positions
            for position in portfolio_positions[symbol]
            if isinstance(position.contract, Stock)
        ]
        total_buying_power = self.get_buying_power(account_summary)
        stock_symbols: Dict[str, PortfolioItem] = {
            stock.contract.symbol: stock for stock in stock_positions
        }

        sell_actions_table = Table(title="Sell-only rebalancing summary")
        sell_actions_table.add_column("Symbol")
        sell_actions_table.add_column("Current shares", justify="right")
        sell_actions_table.add_column("Target shares", justify="right")
        sell_actions_table.add_column("Shares to sell", justify="right")
        sell_actions_table.add_column("Action")

        to_sell: List[Tuple[str, str, int]] = []
        regime_symbols = self._regime_rebalance_symbols()
        symbols = resolve_symbol_configs(self.config, context="sell-only rebalancing")
        sell_only_symbols = [
            symbol
            for symbol in symbols.keys()
            if self.config.is_sell_only_rebalancing(symbol)
            and symbol not in regime_symbols
        ]
        if not sell_only_symbols:
            return (sell_actions_table, to_sell)

        async def check_sell_position_task(symbol: str) -> None:
            ticker = await self.ibkr.get_ticker_for_stock(
                symbol, self.get_primary_exchange(symbol)
            )
            current_position = math.floor(
                stock_symbols[symbol].position if symbol in stock_symbols else 0
            )
            target_value = round(symbols[symbol].weight * total_buying_power, 2)
            market_price = ticker.marketPrice()
            if (
                not market_price
                or math.isnan(market_price)
                or math.isclose(market_price, 0)
            ):
                log.error(
                    f"Invalid market price for {symbol} (market_price={market_price}), skipping for now"
                )
                return
            target_shares = math.floor(target_value / market_price)
            shares_to_sell = current_position - target_shares

            rebalance_policy = self.config.wheel_rebalance_policy(symbol)
            symbol_config = symbols[symbol]
            min_shares = (
                self._as_int_or_none(rebalance_policy.min_threshold_shares)
                or self._as_int_or_none(symbol_config.sell_only_min_threshold_shares)
                or 1
            )
            min_amount = self._as_float_or_none(
                rebalance_policy.min_threshold_amount
            ) or self._as_float_or_none(symbol_config.sell_only_min_threshold_amount)
            min_percent = self._as_float_or_none(
                rebalance_policy.min_threshold_percent
            ) or self._as_float_or_none(symbol_config.sell_only_min_threshold_percent)
            min_percent_relative = self._as_float_or_none(
                rebalance_policy.min_threshold_percent_relative
            ) or self._as_float_or_none(
                symbol_config.sell_only_min_threshold_percent_relative
            )

            if min_percent is not None:
                net_liquidation_value = float(account_summary["NetLiquidation"].value)
                percent_min_amount = net_liquidation_value * min_percent
                min_amount = (
                    max(min_amount, percent_min_amount)
                    if min_amount is not None
                    else percent_min_amount
                )

            if (
                min_percent_relative is not None
                and target_value > 0
                and shares_to_sell > 0
            ):
                current_value = current_position * market_price
                relative_diff = (current_value - target_value) / target_value
                if relative_diff < min_percent_relative:
                    sell_actions_table.add_row(
                        symbol,
                        ifmt(current_position),
                        ifmt(target_shares),
                        ifmt(0),
                        f"[yellow]Below relative threshold {min_percent_relative:.1%} (diff: {relative_diff:.1%})",
                    )
                    return

            if shares_to_sell > 0:
                order_amount = shares_to_sell * market_price
                if min_amount and order_amount < min_amount:
                    sell_actions_table.add_row(
                        symbol,
                        ifmt(current_position),
                        ifmt(target_shares),
                        ifmt(shares_to_sell),
                        f"[yellow]Below min amount ${min_amount:.2f} (would be ${order_amount:.2f})",
                    )
                    return
                if shares_to_sell < min_shares:
                    sell_actions_table.add_row(
                        symbol,
                        ifmt(current_position),
                        ifmt(target_shares),
                        ifmt(shares_to_sell),
                        f"[yellow]Below min shares {min_shares}",
                    )
                    return

                sell_actions_table.add_row(
                    symbol,
                    ifmt(current_position),
                    ifmt(target_shares),
                    ifmt(shares_to_sell),
                    f"[green]Sell {shares_to_sell} shares",
                )
                to_sell.append(
                    (symbol, self.get_primary_exchange(symbol), shares_to_sell)
                )
            else:
                sell_actions_table.add_row(
                    symbol,
                    ifmt(current_position),
                    ifmt(target_shares),
                    ifmt(0),
                    "[cyan]At or below target",
                )

        tasks: List[Coroutine[Any, Any, None]] = [
            check_sell_position_task(symbol) for symbol in sell_only_symbols
        ]
        await log.track_async(tasks, description="Checking sell-only positions...")
        return (sell_actions_table, to_sell)

    async def execute_sell_orders(
        self, sell_orders: List[Tuple[str, str, int]]
    ) -> None:
        for symbol, primary_exchange, quantity in sell_orders:
            try:
                stock_contract = Stock(
                    symbol,
                    self.order_ops.get_order_exchange(),
                    currency="USD",
                    primaryExchange=primary_exchange,
                )
                ticker = await self.ibkr.get_ticker_for_contract(
                    stock_contract,
                    required_fields=[],
                    optional_fields=[TickerField.MIDPOINT, TickerField.MARKET_PRICE],
                )
                limit_price = round(self._midpoint_or_market_price(ticker), 2)
                order = self.order_ops.create_limit_order(
                    action="SELL",
                    quantity=quantity,
                    limit_price=limit_price,
                    transmit=True,
                )
                log.notice(
                    f"Sell-only rebalancing: selling {quantity} shares of {symbol} @ ${limit_price}"
                )
                self.order_ops.enqueue_order(stock_contract, order)
            except Exception as e:
                log.error(
                    f"{symbol}: Failed to execute sell order for {quantity} shares. Error: {e}"
                )
                continue
