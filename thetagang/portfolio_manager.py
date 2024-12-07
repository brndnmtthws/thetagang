import logging
import math
import random
import sys
from asyncio import Future
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from ib_async import (
    AccountValue,
    PortfolioItem,
    TagValue,
    Ticker,
    util,
)
from ib_async.contract import ComboLeg, Contract, Index, Option, Stock
from ib_async.ib import IB
from ib_async.order import LimitOrder
from rich.console import Group
from rich.panel import Panel
from rich.table import Table

from thetagang import log
from thetagang.config import Config
from thetagang.fmt import dfmt, ffmt, ifmt, pfmt
from thetagang.ibkr import IBKR, RequiredFieldValidationError, TickerField
from thetagang.orders import Orders
from thetagang.trades import Trades
from thetagang.util import (
    account_summary_to_dict,
    algo_params_from,
    calculate_net_short_positions,
    can_write_when,
    close_if_unable_to_roll,
    count_long_option_positions,
    count_short_option_positions,
    get_higher_price,
    get_lower_price,
    get_max_dte_for,
    get_minimum_credit,
    get_short_positions,
    get_strike_limit,
    get_target_calls,
    get_target_delta,
    get_target_dte,
    get_write_threshold_perc,
    get_write_threshold_sigma,
    maintain_high_water_mark,
    midpoint_or_market_price,
    net_option_positions,
    portfolio_positions_to_dict,
    position_pnl,
    trading_is_allowed,
    weighted_avg_long_strike,
    weighted_avg_short_strike,
    would_increase_spread,
)

from .options import option_dte

# Turn off some of the more annoying logging output from ib_async
logging.getLogger("ib_async.ib").setLevel(logging.ERROR)
logging.getLogger("ib_async.wrapper").setLevel(logging.CRITICAL)


class NoValidContractsError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)


class PortfolioManager:
    def __init__(
        self,
        config: Config,
        ib: IB,
        completion_future: Future[bool],
        dry_run: bool,
    ) -> None:
        self.account_number = config.account.number
        self.config = config
        self.ibkr = IBKR(
            ib,
            config.ib_async.api_response_wait_time,
            config.orders.exchange,
        )
        self.completion_future = completion_future
        self.has_excess_calls: set[str] = set()
        self.has_excess_puts: set[str] = set()
        self.orders: Orders = Orders()
        self.trades: Trades = Trades(self.ibkr)
        self.target_quantities: Dict[str, int] = {}
        self.qualified_contracts: Dict[int, Contract] = {}
        self.dry_run = dry_run

    def get_short_calls(
        self, portfolio_positions: Dict[str, List[PortfolioItem]]
    ) -> List[PortfolioItem]:
        return self.get_short_contracts(portfolio_positions, "C")

    def get_short_puts(
        self, portfolio_positions: Dict[str, List[PortfolioItem]]
    ) -> List[PortfolioItem]:
        return self.get_short_contracts(portfolio_positions, "P")

    def get_short_contracts(
        self, portfolio_positions: Dict[str, List[PortfolioItem]], right: str
    ) -> List[PortfolioItem]:
        ret: List[PortfolioItem] = []
        for symbol in portfolio_positions:
            ret = ret + get_short_positions(portfolio_positions[symbol], right)
        return ret

    async def put_is_itm(self, contract: Contract) -> bool:
        ticker = await self.ibkr.get_ticker_for_stock(
            contract.symbol, contract.primaryExchange
        )
        return contract.strike >= ticker.marketPrice()

    def position_can_be_closed(self, position: PortfolioItem, table: Table) -> bool:
        if not trading_is_allowed(self.config, position.contract.symbol):
            return False

        close_at_pnl = self.config.roll_when.close_at_pnl
        if close_at_pnl:
            pnl = position_pnl(position)

            if pnl > close_at_pnl:
                table.add_row(
                    f"{position.contract.localSymbol}",
                    "[deep_sky_blue1]Close",
                    f"[deep_sky_blue1]Will be closed because P&L of {pfmt(pnl, 1)} is > {pfmt(close_at_pnl, 1)}",
                )
                return True

        return False

    def put_can_be_closed(self, put: PortfolioItem, table: Table) -> bool:
        return self.position_can_be_closed(put, table)

    async def put_can_be_rolled(self, put: PortfolioItem, table: Table) -> bool:
        # Ignore long positions, we only roll shorts
        if put.position > 0:
            return False

        if not trading_is_allowed(self.config, put.contract.symbol):
            return False

        try:
            itm = await self.put_is_itm(put.contract)
        except RequiredFieldValidationError:
            log.error(
                f"Checking rollable puts failed for #{put.contract.symbol}. Continuing anyway..."
            )
            return False

        if (
            isinstance(put.contract, Option)
            and itm
            and self.config.roll_when.puts.always_when_itm
        ):
            table.add_row(
                f"{put.contract.localSymbol}",
                "[blue]Roll",
                f"[blue]Will be rolled because put is ITM "
                f"and always_when_itm={self.config.roll_when.puts.always_when_itm}",
            )
            return True

        # Check if this put is ITM, and if it's o.k. to roll
        if (
            not self.config.roll_when.puts.itm
            and isinstance(put.contract, Option)
            and itm
        ):
            return False

        # Don't roll if there are excess puts and we're configured not to roll
        if (
            put.contract.symbol in self.has_excess_puts
            and not self.config.roll_when.puts.has_excess
        ):
            table.add_row(
                f"{put.contract.localSymbol}",
                "[cyan1]None",
                "[cyan1]Won't be rolled because there are excess puts",
            )
            return False

        dte = option_dte(put.contract.lastTradeDateOrContractMonth)
        pnl = position_pnl(put)

        roll_when_dte = self.config.roll_when.dte
        roll_when_pnl = self.config.roll_when.pnl
        roll_when_min_pnl = self.config.roll_when.min_pnl

        if self.config.roll_when.max_dte and dte > self.config.roll_when.max_dte:
            return False

        if dte <= roll_when_dte:
            if pnl >= roll_when_min_pnl:
                table.add_row(
                    f"{put.contract.localSymbol}",
                    "[blue]Roll",
                    f"[blue]Can be rolled because DTE of {dte} is <= {self.config.roll_when.dte} and P&L of {pfmt(pnl , 1)} is >= {pfmt(roll_when_min_pnl , 1)}",
                )
                return True
            table.add_row(
                f"{put.contract.localSymbol}",
                "[cyan1]None",
                f"[cyan1]Can't be rolled because P&L of {pfmt(pnl, 1)} is < {pfmt(roll_when_min_pnl, 1)}",
            )

        if pnl >= roll_when_pnl:
            if self.config.roll_when.max_dte is not None:
                table.add_row(
                    f"{put.contract.localSymbol}",
                    "[blue]Roll",
                    f"[blue]Can be rolled because DTE of {dte} is <= {self.config.roll_when.max_dte} and P&L of {pfmt(pnl , 1)} is >= {pfmt(roll_when_pnl , 1)}",
                )
            else:
                table.add_row(
                    f"{put.contract.localSymbol}",
                    "[blue]Roll",
                    f"[blue]Can be rolled because P&L of {pfmt(pnl, 1)} is >= {pfmt(roll_when_pnl, 1)}",
                )
            return True

        return False

    async def call_is_itm(self, contract: Contract) -> bool:
        # Special case for handling VIX
        if contract.symbol == "VIX":
            vix_contract = Index("VIX", "CBOE", "USD")
            ticker = await self.ibkr.get_ticker_for_contract(vix_contract)
        else:
            ticker = await self.ibkr.get_ticker_for_stock(
                contract.symbol, contract.primaryExchange
            )
        return contract.strike <= ticker.marketPrice()

    def call_can_be_closed(self, call: PortfolioItem, table: Table) -> bool:
        return self.position_can_be_closed(call, table)

    async def call_can_be_rolled(self, call: PortfolioItem, table: Table) -> bool:
        # Ignore long positions, we only roll shorts
        if call.position > 0:
            return False

        if not trading_is_allowed(self.config, call.contract.symbol):
            return False

        if (
            isinstance(call.contract, Option)
            and await self.call_is_itm(call.contract)
            and self.config.roll_when.calls.always_when_itm
        ):
            table.add_row(
                f"{call.contract.localSymbol}",
                "[blue]Roll",
                f"[blue]Will be rolled because call is ITM "
                f"and always_when_itm={self.config.roll_when.calls.always_when_itm}",
            )
            return True

        # Check if this call is ITM, and it's o.k. to roll
        if (
            not self.config.roll_when.calls.itm
            and isinstance(call.contract, Option)
            and await self.call_is_itm(call.contract)
        ):
            return False

        # Don't roll if there are excess CCs and we're configured not to roll
        if (
            call.contract.symbol in self.has_excess_calls
            and not self.config.roll_when.calls.has_excess
        ):
            table.add_row(
                f"{call.contract.localSymbol}",
                "[cyan1]None",
                f"[cyan1]Won't be rolled because there are excess calls for {call.contract.symbol}",
            )
            return False

        dte = option_dte(call.contract.lastTradeDateOrContractMonth)
        pnl = position_pnl(call)

        roll_when_dte = self.config.roll_when.dte
        roll_when_pnl = self.config.roll_when.pnl
        roll_when_min_pnl = self.config.roll_when.min_pnl

        if self.config.roll_when.max_dte and dte > self.config.roll_when.max_dte:
            return False

        if dte <= roll_when_dte:
            if pnl >= roll_when_min_pnl:
                table.add_row(
                    f"{call.contract.localSymbol}",
                    "[blue]Roll",
                    f"[blue]Can be rolled because DTE of {dte} is <= {self.config.roll_when.dte}"
                    f" and P&L of {pfmt(pnl , 1)} is >= {pfmt(roll_when_min_pnl , 1)}",
                )
                return True
            table.add_row(
                f"{call.contract.localSymbol}",
                "[cyan1]None",
                f"[cyan1]Can't be rolled because P&L of {pfmt(pnl, 1)} is < {pfmt(roll_when_min_pnl , 1)}",
            )

        if pnl >= roll_when_pnl:
            if self.config.roll_when.max_dte:
                table.add_row(
                    f"{call.contract.localSymbol}",
                    "[blue]Roll",
                    f"[blue]Can be rolled because DTE of {dte} is <= {self.config.roll_when.max_dte}"
                    f" and P&L of {pfmt(pnl , 1)} is >= {pfmt(roll_when_pnl , 1)}",
                )
            else:
                table.add_row(
                    f"{call.contract.localSymbol}",
                    "[blue]Roll",
                    f"[blue]Can be rolled because P&L of {pfmt(pnl, 1)} is >= {pfmt(roll_when_pnl, 1)}",
                )
            return True

        return False

    def get_symbols(self) -> List[str]:
        return list(self.config.symbols.keys())

    def filter_positions(
        self, portfolio_positions: List[PortfolioItem]
    ) -> List[PortfolioItem]:
        symbols = self.get_symbols()
        return [
            item
            for item in portfolio_positions
            if item.account == self.account_number
            and (
                item.contract.symbol in symbols
                or item.contract.symbol == "VIX"
                or item.contract.symbol == self.config.cash_management.cash_fund
            )
            and item.position != 0
            and item.averageCost != 0
        ]

    def get_portfolio_positions(self) -> Dict[str, List[PortfolioItem]]:
        portfolio_positions = self.ibkr.portfolio(account=self.account_number)
        return portfolio_positions_to_dict(self.filter_positions(portfolio_positions))

    def initialize_account(self) -> None:
        self.ibkr.set_market_data_type(self.config.account.market_data_type)

        if self.config.account.cancel_orders:
            # Cancel any existing orders
            open_trades = self.ibkr.open_trades()
            for trade in open_trades:
                if not trade.isDone() and (
                    trade.contract.symbol in self.get_symbols()
                    or (
                        self.config.vix_call_hedge.enabled
                        and trade.contract.symbol == "VIX"
                    )
                    or (
                        self.config.cash_management.enabled
                        and trade.contract.symbol
                        == self.config.cash_management.cash_fund
                    )
                ):
                    log.warning(
                        f"{trade.contract.symbol}: Canceling order {trade.order}"
                    )
                    self.ibkr.cancel_order(trade.order)

    async def summarize_account(
        self,
    ) -> Tuple[
        Dict[str, AccountValue],
        Dict[str, List[PortfolioItem]],
    ]:
        account_summary = await self.ibkr.account_summary(self.account_number)
        account_summary = account_summary_to_dict(account_summary)

        if "NetLiquidation" not in account_summary:
            raise RuntimeError(
                f"Account number {self.config.account.number} appears invalid (no account data returned)"
            )

        table = Table(title="Account summary")
        table.add_column("Item")
        table.add_column("Value", justify="right")
        table.add_row(
            "Net liquidation", dfmt(account_summary["NetLiquidation"].value, 0)
        )
        table.add_row(
            "Excess liquidity", dfmt(account_summary["ExcessLiquidity"].value, 0)
        )
        table.add_row("Initial margin", dfmt(account_summary["InitMarginReq"].value, 0))
        table.add_row(
            "Maintenance margin", dfmt(account_summary["FullMaintMarginReq"].value, 0)
        )
        table.add_row("Buying power", dfmt(account_summary["BuyingPower"].value, 0))
        table.add_row("Total cash", dfmt(account_summary["TotalCashValue"].value, 0))
        table.add_row("Cushion", pfmt(account_summary["Cushion"].value, 0))
        table.add_section()
        table.add_row(
            "Target buying power usage", dfmt(self.get_buying_power(account_summary), 0)
        )
        log.print(Panel(table))

        portfolio_positions = self.get_portfolio_positions()

        position_values: Dict[int, Dict[str, str]] = {}

        async def is_itm(pos: PortfolioItem) -> str:
            if isinstance(pos.contract, Option):
                if pos.contract.right.startswith("C") and await self.call_is_itm(
                    pos.contract
                ):
                    return "✔️"
                if pos.contract.right.startswith("P") and await self.put_is_itm(
                    pos.contract
                ):
                    return "✔️"
            return ""

        async def load_position_task(pos: PortfolioItem) -> None:
            position_values[pos.contract.conId] = {
                "qty": (
                    ifmt(int(pos.position))
                    if pos.position.is_integer()
                    else ffmt(pos.position, 4)
                ),
                "mktprice": dfmt(pos.marketPrice),
                "avgprice": dfmt(pos.averageCost),
                "value": dfmt(pos.marketValue, 0),
                "cost": dfmt(pos.averageCost * pos.position, 0),
                "unrealized": dfmt(pos.unrealizedPNL, 0),
                "p&l": pfmt(position_pnl(pos), 1),
                "itm?": await is_itm(pos),
            }
            if isinstance(pos.contract, Option):
                position_values[pos.contract.conId]["avgprice"] = dfmt(
                    pos.averageCost / float(pos.contract.multiplier)
                )
                position_values[pos.contract.conId]["strike"] = dfmt(
                    pos.contract.strike
                )
                position_values[pos.contract.conId]["dte"] = str(
                    option_dte(pos.contract.lastTradeDateOrContractMonth)
                )
                position_values[pos.contract.conId]["exp"] = str(
                    pos.contract.lastTradeDateOrContractMonth
                )

        tasks = [
            load_position_task(position)
            for _, positions in portfolio_positions.items()
            for position in positions
        ]
        await log.track_async(tasks, "Loading portfolio positions...")

        table = Table(
            title="Portfolio positions",
            collapse_padding=True,
        )
        table.add_column("Symbol")
        table.add_column("R")
        table.add_column("Qty", justify="right")
        table.add_column("MktPrice", justify="right")
        table.add_column("AvgPrice", justify="right")
        table.add_column("Value", justify="right")
        table.add_column("Cost", justify="right")
        table.add_column("Unrealized P&L", justify="right")
        table.add_column("P&L", justify="right")
        table.add_column("Strike", justify="right")
        table.add_column("Exp", justify="right")
        table.add_column("DTE", justify="right")
        table.add_column("ITM?")
        first = True
        for symbol, position in portfolio_positions.items():
            if not first:
                table.add_section()
            first = False
            table.add_row(symbol)
            sorted_positions = sorted(
                position,
                key=lambda p: (
                    option_dte(p.contract.lastTradeDateOrContractMonth)
                    if isinstance(p.contract, Option)
                    else -1
                ),  # Keep stonks on top
            )

            def getval(col: str, conId: int) -> str:
                return position_values[conId][col]

            for pos in sorted_positions:
                conId = pos.contract.conId
                if isinstance(pos.contract, Stock):
                    table.add_row(
                        "",
                        "S",
                        getval("qty", conId),
                        getval("mktprice", conId),
                        getval("avgprice", conId),
                        getval("value", conId),
                        getval("cost", conId),
                        getval("unrealized", conId),
                        getval("p&l", conId),
                    )
                elif isinstance(pos.contract, Option):
                    table.add_row(
                        "",
                        pos.contract.right,
                        getval("qty", conId),
                        getval("mktprice", conId),
                        getval("avgprice", conId),
                        getval("value", conId),
                        getval("cost", conId),
                        getval("unrealized", conId),
                        getval("p&l", conId),
                        getval("strike", conId),
                        getval("exp", conId),
                        getval("dte", conId),
                        getval("itm?", conId),
                    )

        log.print(table)

        return (account_summary, portfolio_positions)

    async def manage(self) -> None:
        try:
            self.initialize_account()
            (account_summary, portfolio_positions) = await self.summarize_account()

            # Check if we have enough buying power to write some puts
            (
                positions_table,
                put_actions_table,
                puts_to_write,
            ) = await self.check_if_can_write_puts(account_summary, portfolio_positions)
            log.print(positions_table)

            # Look for lots of stock that don't have covered calls
            (call_actions_table, calls_to_write) = (
                await self.check_for_uncovered_positions(
                    account_summary, portfolio_positions
                )
            )

            log.print(put_actions_table)
            await self.write_puts(puts_to_write)

            log.print(call_actions_table)
            await self.write_calls(calls_to_write)

            # Refresh positions, in case anything changed from the orders above
            portfolio_positions = self.get_portfolio_positions()

            (rollable_puts, closeable_puts, group1) = await self.check_puts(
                portfolio_positions
            )
            (rollable_calls, closeable_calls, group2) = await self.check_calls(
                portfolio_positions
            )
            log.print(Panel(Group(group1, group2)))

            await self.close_puts(
                closeable_puts + await self.roll_puts(rollable_puts, account_summary)
            )
            await self.close_calls(
                closeable_calls
                + await self.roll_calls(
                    rollable_calls, account_summary, portfolio_positions
                )
            )

            # check if we should do VIX call hedging
            await self.do_vix_hedging(account_summary, portfolio_positions)

            # manage dat cash
            await self.do_cashman(account_summary, portfolio_positions)

            if self.dry_run:
                log.warning("Dry run enabled, no trades will be executed.")

                self.orders.print_summary()
            else:
                self.submit_orders()

                try:
                    await self.ibkr.wait_for_submitting_orders(self.trades.records())
                except RuntimeError:
                    log.error("Submitting orders failed. Continuing anyway..")
                    pass

                await self.adjust_prices()

                await self.ibkr.wait_for_submitting_orders(self.trades.records())

            log.info("ThetaGang is done, shutting down! Cya next time. :sparkles:")
        except:
            log.error("ThetaGang terminated with error...")
            raise

        finally:
            # Shut it down
            self.completion_future.set_result(True)

    async def check_puts(
        self, portfolio_positions: Dict[str, List[PortfolioItem]]
    ) -> Tuple[List[Any], List[Any], Group]:
        # Check for puts which may be rolled to the next expiration or a better price
        puts = self.get_short_puts(portfolio_positions)
        # Filter out an VIX positions
        puts = [put for put in puts if put.contract.symbol != "VIX"]

        # find puts eligible to be rolled or closed
        rollable_puts: List[PortfolioItem] = []
        closeable_puts: List[PortfolioItem] = []

        table = Table(title="Rollable & closeable puts")
        table.add_column("Contract")
        table.add_column("Action")
        table.add_column("Detail")

        async def check_put_can_be_rolled_task(
            put: PortfolioItem, table: Table
        ) -> None:
            if await self.put_can_be_rolled(put, table):
                rollable_puts.append(put)
            elif self.put_can_be_closed(put, table):
                closeable_puts.append(put)

        tasks = [check_put_can_be_rolled_task(put, table) for put in puts]
        await log.track_async(tasks, "Checking rollable/closeable puts...")

        total_rollable_puts = math.floor(sum([abs(p.position) for p in rollable_puts]))
        total_closeable_puts = math.floor(
            sum([abs(p.position) for p in closeable_puts])
        )

        text1 = f"[magenta]{total_rollable_puts} puts can be rolled"
        text2 = f"[magenta]{total_closeable_puts} puts can be closed"

        if total_closeable_puts + total_rollable_puts > 0:
            group = Group(text1, text2, table)
        else:
            group = Group(text1, text2)

        return (rollable_puts, closeable_puts, group)

    async def check_calls(
        self, portfolio_positions: Dict[str, List[PortfolioItem]]
    ) -> Tuple[List[Any], List[Any], Group]:
        # Check for calls which may be rolled to the next expiration or a better price
        calls = self.get_short_calls(portfolio_positions)
        # Filter out an VIX positions
        calls = [call for call in calls if call.contract.symbol != "VIX"]

        # find calls eligible to be rolled
        rollable_calls: List[PortfolioItem] = []
        closeable_calls: List[PortfolioItem] = []

        table = Table(title="Rollable & closeable calls")
        table.add_column("Contract")
        table.add_column("Action")
        table.add_column("Detail")

        for c in log.track(
            calls, description="Checking rollable/closeable calls...", total=len(calls)
        ):
            if await self.call_can_be_rolled(c, table):
                rollable_calls.append(c)
            elif self.call_can_be_closed(c, table):
                closeable_calls.append(c)

        total_rollable_calls = math.floor(
            sum([abs(p.position) for p in rollable_calls])
        )
        total_closeable_calls = math.floor(
            sum([abs(p.position) for p in closeable_calls])
        )

        text1 = f"[magenta]{total_rollable_calls} calls can be rolled"
        text2 = f"[magenta]{total_closeable_calls} calls can be closed"

        if total_closeable_calls + total_rollable_calls > 0:
            group = Group(text1, text2, table)
        else:
            group = Group(text1, text2)

        return (rollable_calls, closeable_calls, group)

    async def get_maximum_new_contracts_for(
        self,
        symbol: str,
        primary_exchange: str,
        account_summary: Dict[str, AccountValue],
    ) -> int:
        total_buying_power = self.get_buying_power(account_summary)
        max_buying_power = (
            self.config.target.maximum_new_contracts_percent * total_buying_power
        )
        ticker = await self.ibkr.get_ticker_for_stock(
            symbol,
            primary_exchange,
        )
        price = midpoint_or_market_price(ticker)

        return max([1, round((max_buying_power / price) // 100)])

    async def check_for_uncovered_positions(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Tuple[Table, List[Tuple[str, str, int, int]]]:
        call_actions_table = Table(title="Call writing summary")
        call_actions_table.add_column("Symbol")
        call_actions_table.add_column("Action")
        call_actions_table.add_column("Detail")
        calculate_net_contracts = self.config.write_when.calculate_net_contracts

        to_write: List[Tuple[str, str, int, int]] = []
        symbols = set(self.get_symbols())

        async def update_to_write_task(symbol: str) -> None:
            if symbol not in symbols:
                # skip positions we don't care about
                return
            short_call_count = (
                calculate_net_short_positions(portfolio_positions[symbol], "C")
                if calculate_net_contracts
                else count_short_option_positions(portfolio_positions[symbol], "C")
            )
            stock_count = math.floor(
                sum(
                    [
                        p.position
                        for p in portfolio_positions[symbol]
                        if isinstance(p.contract, Stock)
                    ]
                )
            )
            strike_limit = math.ceil(
                max(
                    [
                        get_strike_limit(self.config, symbol, "C") or 0,
                    ]
                    + [
                        p.averageCost or 0
                        for p in portfolio_positions[symbol]
                        if isinstance(p.contract, Stock)
                    ]
                )
            )

            target_short_calls = get_target_calls(
                self.config, symbol, stock_count, self.target_quantities[symbol]
            )
            new_contracts_needed = target_short_calls - short_call_count
            excess_calls = short_call_count - target_short_calls

            if excess_calls > 0:
                self.has_excess_calls.add(symbol)
                call_actions_table.add_row(
                    symbol,
                    "[yellow]None",
                    f"[yellow]Warning: excess_calls={excess_calls} stock_count={stock_count},"
                    f" short_call_count={short_call_count}, target_short_calls={target_short_calls}",
                )

            maximum_new_contracts = await self.get_maximum_new_contracts_for(
                symbol,
                self.get_primary_exchange(symbol),
                account_summary,
            )
            calls_to_write = max(
                [0, min([new_contracts_needed, maximum_new_contracts])]
            )

            ticker = await self.ibkr.get_ticker_for_stock(
                symbol, self.get_primary_exchange(symbol)
            )

            (write_threshold, absolute_daily_change) = (None, None)

            async def is_ok_to_write_calls(
                symbol: str,
                ticker: Optional[Ticker],
                calls_to_write: int,
            ) -> bool:
                nonlocal write_threshold, absolute_daily_change
                if (
                    not ticker
                    or calls_to_write <= 0
                    or not trading_is_allowed(self.config, symbol)
                ):
                    return False

                (can_write_when_green, can_write_when_red) = can_write_when(
                    self.config, symbol, "C"
                )

                if not can_write_when_green and ticker.marketPrice() > ticker.close:
                    call_actions_table.add_row(
                        symbol,
                        "[cyan1]None",
                        f"[cyan1]Skipping because can_write_when_green={can_write_when_green} and marketPrice={ticker.marketPrice():.2f} > close={ticker.close}",
                    )
                    return False
                if not can_write_when_red and ticker.marketPrice() < ticker.close:
                    call_actions_table.add_row(
                        symbol,
                        "[cyan1]None",
                        f"[cyan1]Skipping because can_write_when_red={can_write_when_red} and marketPrice={ticker.marketPrice():.2f} < close={ticker.close}",
                    )
                    return False

                (write_threshold, absolute_daily_change) = (
                    await self.get_write_threshold(ticker, "C")
                )
                if absolute_daily_change < write_threshold:
                    call_actions_table.add_row(
                        symbol,
                        "[cyan1]None",
                        f"[cyan1]Need to write {calls_to_write} calls, "
                        f"but skipping because absolute_daily_change={absolute_daily_change:.2f}"
                        f" less than write_threshold={write_threshold:.2f}",
                    )
                    return False
                return True

            ok_to_write = await is_ok_to_write_calls(symbol, ticker, calls_to_write)
            strike_limit = math.ceil(max([strike_limit, ticker.marketPrice()]))

            if calls_to_write > 0 and ok_to_write:
                call_actions_table.add_row(
                    symbol,
                    "[green]Write",
                    f"[green]Will write {calls_to_write} calls, {new_contracts_needed} needed, "
                    f"limited to {maximum_new_contracts} new contracts, at or above strike {dfmt(strike_limit)}"
                    f" (target_short_calls={target_short_calls} short_call_count={short_call_count} "
                    f"absolute_daily_change={absolute_daily_change:.2f} write_threshold={write_threshold:.2f})",
                )
                to_write.append(
                    (
                        symbol,
                        self.get_primary_exchange(symbol),
                        calls_to_write,
                        strike_limit,
                    )
                )

        tasks = [update_to_write_task(symbol) for symbol in portfolio_positions]
        await log.track_async(tasks, description="Checking for uncovered positions...")

        return (call_actions_table, to_write)

    async def write_calls(self, calls: List[Any]) -> None:
        for symbol, primary_exchange, quantity, strike_limit in calls:
            try:
                sell_ticker = await self.find_eligible_contracts(
                    Stock(
                        symbol,
                        self.get_order_exchange(),
                        currency="USD",
                        primaryExchange=primary_exchange,
                    ),
                    "C",
                    strike_limit,
                    minimum_price=lambda: get_minimum_credit(self.config),
                )
            except (RuntimeError, NoValidContractsError):
                log.error(
                    f"{symbol}: Finding eligible contracts failed. Continuing anyway..."
                )
                continue

            # Create order
            order = LimitOrder(
                "SELL",
                quantity,
                round(get_higher_price(sell_ticker), 2),
                algoStrategy=self.get_algo_strategy(),
                algoParams=self.get_algo_params(),
                tif="DAY",
                account=self.account_number,
            )

            # Enqueue order
            self.enqueue_order(sell_ticker.contract, order)

    async def write_puts(
        self, puts: List[Tuple[str, str, int, Optional[float]]]
    ) -> None:
        for symbol, primary_exchange, quantity, strike_limit in puts:
            try:
                sell_ticker = await self.find_eligible_contracts(
                    Stock(
                        symbol,
                        self.get_order_exchange(),
                        currency="USD",
                        primaryExchange=primary_exchange,
                    ),
                    "P",
                    strike_limit,
                    minimum_price=lambda: get_minimum_credit(self.config),
                )
            except (RuntimeError, NoValidContractsError):
                log.error(
                    f"{symbol}: Finding eligible contracts failed. Continuing anyway..."
                )
                continue

            # Create order
            order = LimitOrder(
                "SELL",
                quantity,
                round(get_higher_price(sell_ticker), 2),
                algoStrategy=self.get_algo_strategy(),
                algoParams=self.get_algo_params(),
                tif="DAY",
                account=self.account_number,
            )

            # Enqueue order
            self.enqueue_order(sell_ticker.contract, order)

    def get_primary_exchange(self, symbol: str) -> str:
        return self.config.symbols[symbol].primary_exchange

    def get_buying_power(self, account_summary: Dict[str, AccountValue]) -> int:
        return math.floor(
            float(account_summary["NetLiquidation"].value)
            * self.config.account.margin_usage
        )

    async def check_if_can_write_puts(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Tuple[Table, Table, List[Tuple[str, str, int, Optional[float]]]]:
        # Get stock positions
        stock_positions = [
            position
            for symbol in portfolio_positions
            for position in portfolio_positions[symbol]
            if isinstance(position.contract, Stock)
        ]

        total_buying_power = self.get_buying_power(account_summary)

        stock_symbols: Dict[str, PortfolioItem] = dict()
        for stock in stock_positions:
            symbol = stock.contract.symbol
            stock_symbols[symbol] = stock

        targets: Dict[str, float] = dict()
        target_additional_quantity: Dict[str, Dict[str, int | bool]] = dict()

        calculate_net_contracts = self.config.write_when.calculate_net_contracts

        positions_summary_table = Table(
            title="Positions summary",
            show_edge=False,
        )
        positions_summary_table.add_column("Symbol")
        positions_summary_table.add_column("Shares", justify="right")
        positions_summary_table.add_column("Short puts", justify="right")
        positions_summary_table.add_column("Long puts", justify="right")
        if calculate_net_contracts:
            positions_summary_table.add_column("Net short puts", justify="right")
        positions_summary_table.add_column("Short calls", justify="right")
        positions_summary_table.add_column("Long calls", justify="right")
        if calculate_net_contracts:
            positions_summary_table.add_column("Net short calls", justify="right")
        positions_summary_table.add_column("Target value", justify="right")
        positions_summary_table.add_column("Target share qty", justify="right")
        positions_summary_table.add_column("Net target shares", justify="right")
        positions_summary_table.add_column("Net target contracts", justify="right")

        put_actions_table = Table(title="Put writing summary")
        put_actions_table.add_column("Symbol")
        put_actions_table.add_column("Action")
        put_actions_table.add_column("Detail")

        async def calculate_target_position_task(symbol: str) -> None:
            ticker = await self.ibkr.get_ticker_for_stock(
                symbol, self.get_primary_exchange(symbol)
            )

            current_position = math.floor(
                stock_symbols[symbol].position if symbol in stock_symbols else 0
            )

            targets[symbol] = round(
                self.config.symbols[symbol].weight * total_buying_power, 2
            )
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
            self.target_quantities[symbol] = math.floor(targets[symbol] / market_price)

            if symbol in portfolio_positions:
                # Current number of puts
                net_short_put_count = short_put_count = count_short_option_positions(
                    portfolio_positions[symbol], "P"
                )
                short_put_avg_strike = weighted_avg_short_strike(
                    portfolio_positions[symbol], "P"
                )
                long_put_count = count_long_option_positions(
                    portfolio_positions[symbol], "P"
                )
                long_put_avg_strike = weighted_avg_long_strike(
                    portfolio_positions[symbol], "P"
                )
                # Current number of calls
                net_short_call_count = short_call_count = count_short_option_positions(
                    portfolio_positions[symbol], "C"
                )
                short_call_avg_strike = weighted_avg_short_strike(
                    portfolio_positions[symbol], "C"
                )
                long_call_count = count_long_option_positions(
                    portfolio_positions[symbol], "C"
                )
                long_call_avg_strike = weighted_avg_long_strike(
                    portfolio_positions[symbol], "C"
                )

                if calculate_net_contracts:
                    net_short_put_count = calculate_net_short_positions(
                        portfolio_positions[symbol], "P"
                    )
                    net_short_call_count = calculate_net_short_positions(
                        portfolio_positions[symbol], "C"
                    )
            else:
                net_short_put_count = short_put_count = long_put_count = 0
                short_put_avg_strike = long_put_avg_strike = None
                net_short_call_count = short_call_count = long_call_count = 0
                short_call_avg_strike = long_call_avg_strike = None

            qty_to_write = math.floor(
                self.target_quantities[symbol]
                - current_position
                - 100 * net_short_put_count
            )
            net_target_shares = qty_to_write
            net_target_puts = net_target_shares // 100

            if calculate_net_contracts:
                positions_summary_table.add_row(
                    symbol,
                    ifmt(current_position),
                    ifmt(short_put_count),
                    ifmt(long_put_count),
                    ifmt(net_short_put_count),
                    ifmt(short_call_count),
                    ifmt(long_call_count),
                    ifmt(net_short_call_count),
                    dfmt(targets[symbol]),
                    ifmt(self.target_quantities[symbol]),
                    ifmt(net_target_shares),
                    ifmt(net_target_puts),
                )
                positions_summary_table.add_row(
                    "",
                    "",
                    dfmt(short_put_avg_strike),
                    dfmt(long_put_avg_strike),
                    "",
                    dfmt(short_call_avg_strike),
                    dfmt(long_call_avg_strike),
                )
            else:
                positions_summary_table.add_row(
                    symbol,
                    ifmt(current_position),
                    ifmt(short_put_count),
                    ifmt(long_put_count),
                    ifmt(short_call_count),
                    ifmt(long_call_count),
                    dfmt(targets[symbol]),
                    ifmt(self.target_quantities[symbol]),
                    ifmt(net_target_shares),
                    ifmt(net_target_puts),
                )
                positions_summary_table.add_row(
                    "",
                    "",
                    dfmt(short_put_avg_strike),
                    dfmt(long_put_avg_strike),
                    dfmt(short_call_avg_strike),
                    dfmt(long_call_avg_strike),
                )
            positions_summary_table.add_section()

            async def is_ok_to_write_puts(
                symbol: str,
                ticker: Ticker,
                puts_to_write: int,
            ) -> bool:
                if puts_to_write <= 0 or not trading_is_allowed(self.config, symbol):
                    return False

                (can_write_when_green, can_write_when_red) = can_write_when(
                    self.config, symbol, "P"
                )

                if not can_write_when_green and ticker.marketPrice() > ticker.close:
                    put_actions_table.add_row(
                        symbol,
                        "[cyan1]None",
                        f"[cyan1]Skipping because can_write_when_green={can_write_when_green} and marketPrice={ticker.marketPrice():.2f} > close={ticker.close}",
                    )
                    return False
                if not can_write_when_red and ticker.marketPrice() < ticker.close:
                    put_actions_table.add_row(
                        symbol,
                        "[cyan1]None",
                        f"[cyan1]Skipping because can_write_when_red={can_write_when_red} and marketPrice={ticker.marketPrice():.2f} < close={ticker.close}",
                    )
                    return False

                (write_threshold, absolute_daily_change) = (
                    await self.get_write_threshold(ticker, "P")
                )
                if absolute_daily_change < write_threshold:
                    put_actions_table.add_row(
                        symbol,
                        "[cyan1]None",
                        f"[cyan1]Need to write {puts_to_write} puts, but skipping because absolute_daily_change={absolute_daily_change:.2f} less than write_threshold={write_threshold:.2f}[/cyan1]",
                    )
                    return False
                return True

            ok_to_write = await is_ok_to_write_puts(symbol, ticker, net_target_puts)

            target_additional_quantity[symbol] = {
                "qty": net_target_puts,
                "ok_to_write": ok_to_write,
            }

        tasks = [
            calculate_target_position_task(symbol)
            for symbol in self.config.symbols.keys()
        ]
        await log.track_async(tasks, description="Calculating target positions...")

        to_write: List[Tuple[str, str, int, Optional[float]]] = []

        async def update_to_write_task(
            symbol: str, target: Dict[str, int | bool]
        ) -> None:
            ok_to_write = target["ok_to_write"]
            additional_quantity = target["qty"]
            # NOTE: it's possible there are non-standard option contract sizes,
            # like with futures, but we don't bother handling those cases.
            # Please don't use this code with futures.
            if additional_quantity >= 1 and ok_to_write:
                maximum_new_contracts = await self.get_maximum_new_contracts_for(
                    symbol,
                    self.get_primary_exchange(symbol),
                    account_summary,
                )
                puts_to_write = min([additional_quantity, maximum_new_contracts])
                if puts_to_write > 0:
                    strike_limit = get_strike_limit(self.config, symbol, "P")
                    if strike_limit:
                        put_actions_table.add_row(
                            symbol,
                            "[green]Write",
                            f"[green]Will write {puts_to_write} puts, {additional_quantity}"
                            f" needed, capped at {maximum_new_contracts}, at or below strike ${strike_limit}",
                        )
                    else:
                        put_actions_table.add_row(
                            symbol,
                            "[green]Write",
                            f"[green]Will write {puts_to_write} puts, {additional_quantity}"
                            f" needed, capped at {maximum_new_contracts}",
                        )
                    to_write.append(
                        (
                            symbol,
                            self.get_primary_exchange(symbol),
                            puts_to_write,
                            strike_limit,
                        )
                    )
            elif additional_quantity < 0:
                self.has_excess_puts.add(symbol)
                put_actions_table.add_row(
                    symbol,
                    "[yellow]None",
                    "[yellow]Warning: excess positions based "
                    "on net liquidation and target margin usage",
                )

        tasks = [
            update_to_write_task(symbol, target)
            for symbol, target in target_additional_quantity.items()
        ]
        await log.track_async(tasks, description="Generating positions summary...")

        return (positions_summary_table, put_actions_table, to_write)

    async def close_puts(self, puts: List[PortfolioItem]) -> None:
        return await self.close_positions("P", puts)

    async def roll_puts(
        self,
        puts: List[PortfolioItem],
        account_summary: Dict[str, AccountValue],
    ) -> List[PortfolioItem]:
        return await self.roll_positions(puts, "P", account_summary)

    async def close_calls(self, calls: List[PortfolioItem]) -> None:
        return await self.close_positions("C", calls)

    async def roll_calls(
        self,
        calls: List[PortfolioItem],
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> List[PortfolioItem]:
        return await self.roll_positions(
            calls, "C", account_summary, portfolio_positions
        )

    async def close_positions(self, right: str, positions: List[PortfolioItem]) -> None:
        log.notice(f"Close {right} positions...")
        for position in positions:
            try:
                position.contract.exchange = self.get_order_exchange()
                ticker = await self.ibkr.get_ticker_for_contract(position.contract)
                is_short = position.position < 0
                price = (
                    round(get_lower_price(ticker), 2)
                    if is_short
                    else round(get_higher_price(ticker), 2)
                )
                if util.isNan(price) or math.isnan(price) or not price:
                    # if the price is near zero or NaN, use the minimum price
                    log.warning(
                        f"Market price data unavailable for {position.contract.localSymbol}, using ticker.minTick={ticker.minTick}"
                    )
                    price = ticker.minTick

                qty = abs(position.position)
                order = LimitOrder(
                    "BUY" if is_short else "SELL",
                    qty,
                    price,
                    algoStrategy=self.get_algo_strategy(),
                    algoParams=self.get_algo_params(),
                    tif="DAY",
                    account=self.account_number,
                )

                self.enqueue_order(ticker.contract, order)
            except RuntimeError:
                log.error(
                    "Error occurred when trying to close position. Continuing anyway..."
                )
                continue

    async def roll_positions(
        self,
        positions: List[PortfolioItem],
        right: str,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Optional[Dict[str, List[PortfolioItem]]] = None,
    ) -> List[PortfolioItem]:
        closeable_positions: List[PortfolioItem] = []

        log.notice(f"Rolling {right} positions...")

        for position in positions:
            try:
                symbol = position.contract.symbol

                position.contract.exchange = self.get_order_exchange()
                buy_ticker = await self.ibkr.get_ticker_for_contract(
                    position.contract,
                    required_fields=[],
                    optional_fields=[TickerField.MIDPOINT, TickerField.MARKET_PRICE],
                )

                strike_limit = get_strike_limit(self.config, symbol, right)
                if right.startswith("C"):
                    average_cost = (
                        [
                            p.averageCost
                            for p in portfolio_positions[symbol]
                            if isinstance(p.contract, Stock)
                        ]
                        if portfolio_positions and symbol in portfolio_positions
                        else [0]
                    )
                    strike_limit = round(
                        max([strike_limit or 0] + average_cost),
                        2,
                    )
                    if maintain_high_water_mark(self.config, symbol):
                        strike_limit = max([strike_limit, position.contract.strike])

                elif right.startswith("P"):
                    strike_limit = round(
                        min(
                            [strike_limit or sys.float_info.max]
                            + [
                                max(
                                    [
                                        position.contract.strike,
                                        position.contract.strike
                                        + (
                                            position.averageCost
                                            / float(position.contract.multiplier)
                                        )
                                        - midpoint_or_market_price(buy_ticker),
                                    ]
                                )
                            ]
                        ),
                        2,
                    )
                    # special case: if we're rolling a put that's ITM, we want to roll to an equal or lower strike, not higher
                    if isinstance(position.contract, Option) and await self.put_is_itm(
                        position.contract
                    ):
                        strike_limit = min([strike_limit, position.contract.strike])

                kind = "calls" if right.startswith("C") else "puts"

                minimum_price = (
                    (lambda: get_minimum_credit(self.config))
                    if not getattr(self.config.roll_when, kind).credit_only
                    else (
                        lambda: midpoint_or_market_price(buy_ticker)
                        + get_minimum_credit(self.config)
                    )
                )

                def fallback_minimum_price() -> float:
                    return midpoint_or_market_price(buy_ticker)

                sell_ticker = await self.find_eligible_contracts(
                    Stock(
                        symbol,
                        self.get_order_exchange(),
                        "USD",
                        primaryExchange=self.get_primary_exchange(symbol),
                    ),
                    right,
                    strike_limit,
                    exclude_expirations_before=position.contract.lastTradeDateOrContractMonth,
                    exclude_exp_strike=(
                        position.contract.strike,
                        position.contract.lastTradeDateOrContractMonth,
                    ),
                    minimum_price=minimum_price,
                    fallback_minimum_price=fallback_minimum_price,
                )
                if not sell_ticker.contract:
                    raise RuntimeError(f"Invalid ticker (no contract): {sell_ticker}")

                qty_to_roll = math.floor(abs(position.position))
                maximum_new_contracts = await self.get_maximum_new_contracts_for(
                    symbol,
                    self.get_primary_exchange(symbol),
                    account_summary,
                )
                from_dte = option_dte(position.contract.lastTradeDateOrContractMonth)
                roll_when_dte = self.config.roll_when.dte
                if from_dte > roll_when_dte:
                    qty_to_roll = min([qty_to_roll, maximum_new_contracts])

                price = midpoint_or_market_price(buy_ticker) - midpoint_or_market_price(
                    sell_ticker
                )
                # a buy order should be at most the minimum price, when we expect a credit
                price = (
                    min([price, -get_minimum_credit(self.config)])
                    if getattr(self.config.roll_when, kind).credit_only
                    else price
                )

                # store a copy of the contracts so we can retrieve them later by conId
                self.qualified_contracts[position.contract.conId] = position.contract
                self.qualified_contracts[sell_ticker.contract.conId] = (
                    sell_ticker.contract
                )

                # Create combo legs
                comboLegs = [
                    ComboLeg(
                        conId=position.contract.conId,
                        ratio=1,
                        exchange=self.get_order_exchange(),
                        action="BUY",
                    ),
                    ComboLeg(
                        conId=sell_ticker.contract.conId,
                        ratio=1,
                        exchange=self.get_order_exchange(),
                        action="SELL",
                    ),
                ]

                # Create contract
                combo = Contract(
                    secType="BAG",
                    symbol=symbol,
                    currency="USD",
                    exchange=self.get_order_exchange(),
                    comboLegs=comboLegs,
                )

                # Create order
                order = LimitOrder(
                    "BUY",
                    qty_to_roll,
                    round(price, 2),
                    algoStrategy=self.get_algo_strategy(),
                    algoParams=self.get_algo_params(),
                    tif="DAY",
                    account=self.account_number,
                )

                to_dte = option_dte(sell_ticker.contract.lastTradeDateOrContractMonth)
                from_strike = position.contract.strike
                to_strike = sell_ticker.contract.strike
                log.info(
                    f"{symbol}: Rolling from_strike={from_strike} to_strike={to_strike} from_dte={from_dte} to_dte={to_dte} price={dfmt(price,3)} qty_to_roll={qty_to_roll}"
                )

                # Enqueue order
                self.enqueue_order(combo, order)
            except NoValidContractsError:
                dte = option_dte(position.contract.lastTradeDateOrContractMonth)
                if (
                    close_if_unable_to_roll(self.config, position.contract.symbol)
                    and self.config.roll_when.max_dte
                    and dte <= self.config.roll_when.max_dte
                    and position_pnl(position) > 0
                ):
                    log.warning(
                        f"{position.contract.symbol}: Unable to find a suitable contract to roll to for {position.contract.localSymbol}. Closing position instead..."
                    )
                    closeable_positions.append(position)
                    continue
                else:
                    log.error(
                        f"{position.contract.symbol}: Error occurred when trying to roll position. Continuing anyway..."
                    )
            except RuntimeError:
                log.error(
                    f"{position.contract.symbol}: Error occurred when trying to roll position. Continuing anyway..."
                )
                continue

        return closeable_positions

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
            target_dte if target_dte else get_target_dte(self.config, underlying.symbol)
        )
        contract_target_delta: float = (
            target_delta
            if target_delta
            else get_target_delta(self.config, underlying.symbol, right)
        )
        contract_max_dte = get_max_dte_for(underlying.symbol, self.config)

        log.notice(
            f"{underlying.symbol}: Searching option chain for "
            f"right={right} strike_limit={strike_limit} minimum_price={dfmt(minimum_price(),3)} "
            f"fallback_minimum_price={dfmt(fallback_minimum_price() if fallback_minimum_price else 0,3)} "
            f"contract_target_dte={contract_target_dte} contract_max_dte={contract_max_dte} "
            f"contract_target_delta={contract_target_delta}, "
            "this can take a while...",
        )

        underlying_ticker = await self.ibkr.get_ticker_for_contract(underlying)

        underlying_price = midpoint_or_market_price(underlying_ticker)

        chains = await self.ibkr.get_chains_for_contract(underlying)

        chain = next(c for c in chains if c.exchange == underlying.exchange)

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

        chain_expirations = self.config.option_chains.expirations
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
                f"No valid contract expirations found for {underlying.symbol}. Continuing anyway...",
            )
        rights = [right]

        def nearest_strikes(strikes: List[float]) -> List[float]:
            chain_strikes = self.config.option_chains.strikes
            if right.startswith("P"):
                return strikes[-chain_strikes:]
            return strikes[:chain_strikes]

        strikes = nearest_strikes(strikes)
        if len(strikes) < 1:
            raise NoValidContractsError(
                f"No valid contract strikes found for {underlying.symbol}. Continuing anyway...",
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
                self.get_order_exchange(),
                # tradingClass=chain.tradingClass,
            )
            for right in rights
            for expiration in expirations
            for strike in strikes
        ]

        contracts = await self.ibkr.qualify_contracts(*contracts)

        # exclude strike, but only for the first exp
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
            # The open interest value is never present when using historical
            # data, so just ignore it when the value is None
            if right.startswith("P"):
                return ticker.putOpenInterest >= minimum_open_interest
            if right.startswith("C"):
                return ticker.callOpenInterest >= minimum_open_interest
            return False

        def delta_is_valid(ticker: Ticker) -> bool:
            return (
                ticker.modelGreeks is not None
                and ticker.modelGreeks
                and ticker.modelGreeks.delta is not None
                and not util.isNan(ticker.modelGreeks.delta)
                and abs(ticker.modelGreeks.delta) <= contract_target_delta
            )

        def price_is_valid(ticker: Ticker) -> bool:
            def cost_doesnt_exceed_market_price(ticker: Ticker) -> bool:
                # when writing puts, we need to be sure that the strike +
                # credit is less than or equal to the current market price, so
                # that we don't exceed the target capital allocation for this
                # position
                return (
                    right.startswith("C")
                    or isinstance(ticker.contract, Option)
                    and ticker.contract.strike
                    <= midpoint_or_market_price(ticker) + underlying_price
                )

            return midpoint_or_market_price(
                ticker
            ) > minimum_price() and cost_doesnt_exceed_market_price(ticker)

        # Filter out invalid price
        tickers = [
            ticker
            for ticker in log.track(
                tickers,
                description=f"{underlying.symbol}: Filtering invalid prices...",
                total=len(tickers),
            )
            if price_is_valid(ticker)
        ]

        # Filter out invalid greeks
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
            minimum_open_interest = self.config.target.minimum_open_interest

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

            # Sort by delta first, then expiry date
            tickers = sorted(
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

            return tickers

        tickers = filter_remaining_tickers(list(tickers), True)

        the_chosen_ticker = None

        if len(tickers) == 0:
            if not math.isclose(minimum_price(), 0.0):
                # if we arrive here, it means that 1) we expect to roll for a
                # credit only, but 2) we didn't find any suitable contracts,
                # most likely because we can't roll out and up/down to the
                # target delta
                #
                # because of this, we'll allow rolling to a less-than-optimal
                # strike, provided it's still a credit
                tickers = filter_remaining_tickers(list(delta_reject_tickers), False)
            if len(tickers) < 1:
                # if there are _still_ no tickers remaining, there's nothing
                # more we can do
                raise NoValidContractsError(
                    f"No valid contracts found for {underlying.symbol}. Continuing anyway...",
                )
        elif fallback_minimum_price is not None:
            # if there's a fallback minimum price specified, try to find
            # contracts that are at least that price first
            for ticker in tickers:
                if midpoint_or_market_price(ticker) > fallback_minimum_price():
                    the_chosen_ticker = ticker
                    break
            if the_chosen_ticker is None:
                # uh of, if we make it here then all of these options are
                # net debits, so let's at least choose the ticker that will
                # result in the smallest debit (i.e., minimize the max loss)
                tickers = sorted(tickers, key=midpoint_or_market_price, reverse=True)

        if the_chosen_ticker is None:
            # fall back to the first suitable result
            the_chosen_ticker = tickers[0]

        if not the_chosen_ticker or not the_chosen_ticker.contract:
            raise RuntimeError(
                f"{underlying.symbol}: Something went wrong, the_chosen_ticker={the_chosen_ticker}"
            )

        log.notice(
            f"{underlying.symbol}: Found suitable contract at "
            f"strike={the_chosen_ticker.contract.strike} "
            f"dte={option_dte(the_chosen_ticker.contract.lastTradeDateOrContractMonth)} "
            f"price={dfmt(midpoint_or_market_price(the_chosen_ticker),3)}"
        )

        return the_chosen_ticker

    def get_algo_strategy(self) -> str:
        return self.config.orders.algo.strategy

    def get_algo_params(self) -> List[TagValue]:
        return algo_params_from(self.config.orders.algo.params)

    def get_order_exchange(self) -> str:
        return self.config.orders.exchange

    async def do_vix_hedging(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> None:
        log.notice("VIX: Checking on our VIX call hedge...")

        async def inner_handler() -> None:
            if not self.config.vix_call_hedge.enabled:
                log.warning("🛑 VIX call hedging not enabled, skipping...")
                return None

            async def vix_calls_should_be_closed() -> (
                tuple[bool, Optional[Ticker], Optional[float]]
            ):
                if self.config.vix_call_hedge.close_hedges_when_vix_exceeds:
                    vix_contract = Index("VIX", "CBOE", "USD")
                    vix_ticker = await self.ibkr.get_ticker_for_contract(vix_contract)
                    close_hedges_when_vix_exceeds = (
                        self.config.vix_call_hedge.close_hedges_when_vix_exceeds
                    )
                    if vix_ticker.marketPrice() > close_hedges_when_vix_exceeds:
                        return (True, vix_ticker, close_hedges_when_vix_exceeds)
                    return (False, vix_ticker, close_hedges_when_vix_exceeds)
                return (False, None, None)

            ignore_dte = self.config.vix_call_hedge.ignore_dte

            net_vix_call_count = net_option_positions(
                "VIX", portfolio_positions, "C", ignore_dte=ignore_dte
            )
            if net_vix_call_count > 0:
                log.info(
                    f"[bold blue_violet]VIX: net_vix_call_count={net_vix_call_count} "
                    f"(DTE <= {ignore_dte} contracts ignored), "
                    "checking if we need to close positions...",
                )
                (
                    close_vix_calls,
                    vix_ticker,
                    close_hedges_when_vix_exceeds,
                ) = await vix_calls_should_be_closed()
                if close_vix_calls and vix_ticker and close_hedges_when_vix_exceeds:
                    log.info(
                        f"VIX: VIX={vix_ticker.marketPrice():.2f}, which exceeds "
                        f"vix_call_hedge.close_hedges_when_vix_exceeds={close_hedges_when_vix_exceeds}, "
                        "checking if we need to close positions...",
                    )
                    for position in portfolio_positions["VIX"]:
                        if (
                            position.contract.right.startswith("C")
                            and position.position < 0
                        ):
                            # only applies to long calls
                            continue
                        log.notice(
                            f"Creating closing order for {position.contract.localSymbol}..."
                        )
                        position.contract.exchange = self.get_order_exchange()
                        sell_ticker = await self.ibkr.get_ticker_for_contract(
                            position.contract
                        )
                        price = round(get_lower_price(sell_ticker), 2)
                        qty = abs(position.position)
                        order = LimitOrder(
                            "SELL",
                            qty,
                            price,
                            algoStrategy=self.get_algo_strategy(),
                            algoParams=self.get_algo_params(),
                            tif="DAY",
                            account=self.account_number,
                        )

                        self.enqueue_order(sell_ticker.contract, order)

                log.info(
                    f"VIX: net_vix_call_count={net_vix_call_count}, no action is needed at this time",
                )
                return

            log.info(
                f"VIX: net_vix_call_count={net_vix_call_count}, checking if we should open new positions...",
            )

            (
                close_vix_calls,
                vix_ticker,
                close_hedges_when_vix_exceeds,
            ) = await vix_calls_should_be_closed()
            # we never want to write calls if we're simultaneously ready to close calls
            if not close_vix_calls:
                try:
                    vixmo_contract = Index("VIXMO", "CBOE", "USD")
                    vixmo_ticker = await self.ibkr.get_ticker_for_contract(
                        vixmo_contract
                    )

                    weight = 0.0

                    for allocation in self.config.vix_call_hedge.allocation:
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

                    log.info(
                        f"VIX: VIXMO={vixmo_ticker.marketPrice():.2f}, target call hedge weight={weight}",
                    )

                    allocation_amount = (
                        float(account_summary["NetLiquidation"].value) * weight
                    )
                    delta = self.config.vix_call_hedge.delta
                    target_dte = self.config.vix_call_hedge.target_dte
                    if weight > 0:
                        log.notice(
                            f"VIX: Current VIXMO spot price prescribes an allocation of up to "
                            f"${allocation_amount:.2f} for purchasing VIX calls, "
                            f"at or above delta={delta} with a DTE >= {target_dte}",
                        )
                    else:
                        log.info(
                            "VIX: Based on current VIXMO value and rules, no action is needed",
                        )
                        return

                    log.info(
                        "VIX: Scanning option chain for eligible contracts...",
                    )
                    vix_contract = Index("VIX", "CBOE", "USD")
                    buy_ticker = await self.find_eligible_contracts(
                        vix_contract,
                        "C",
                        0,
                        target_delta=delta,
                        target_dte=target_dte,
                        minimum_price=lambda: get_minimum_credit(self.config),
                    )
                    if not isinstance(buy_ticker.contract, Option):
                        raise RuntimeError(
                            f"Something went wrong, buy_ticker={buy_ticker}"
                        )
                    price = round(get_lower_price(buy_ticker), 2)
                    qty = math.floor(
                        allocation_amount
                        / price
                        / float(buy_ticker.contract.multiplier)
                    )

                    order = LimitOrder(
                        "BUY",
                        qty,
                        price,
                        algoStrategy=self.get_algo_strategy(),
                        algoParams=self.get_algo_params(),
                        tif="DAY",
                        account=self.account_number,
                    )

                    self.enqueue_order(buy_ticker.contract, order)
                except (RuntimeError, NoValidContractsError):
                    log.error(
                        "VIX: Error occurred when VIX call hedging. Continuing anyway..."
                    )

        await inner_handler()

    def calc_pending_cash_balance(self) -> float:
        def get_multiplier(contract: Contract) -> float:
            if contract.secType == "BAG":
                # with combos/bag orders we'll use the _first_ multiplier, for
                # simplicity, because we only create combos with equal legs
                return float(
                    self.qualified_contracts[contract.comboLegs[0].conId].multiplier
                )
            return float(contract.multiplier)

        return sum(
            [
                order.lmtPrice * order.totalQuantity * get_multiplier(contract)
                for (contract, order) in self.orders.records()
                if order.action == "SELL"
            ]
        ) - sum(
            [
                order.lmtPrice * order.totalQuantity * get_multiplier(contract)
                for (contract, order) in self.orders.records()
                if order.action == "BUY"
            ]
        )

    async def do_cashman(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> None:
        log.notice("Cash management...")

        async def inner_handler() -> None:
            if not self.config.cash_management.enabled:
                log.warning(
                    "🛑 Cash management not enabled, skipping",
                )
                return None

            target_cash_balance = self.config.cash_management.target_cash_balance
            buy_threshold = self.config.cash_management.buy_threshold
            sell_threshold = self.config.cash_management.sell_threshold
            cash_balance = math.floor(float(account_summary["TotalCashValue"].value))
            pending_balance = self.calc_pending_cash_balance()

            try:

                async def make_order() -> tuple[Optional[Ticker], Optional[LimitOrder]]:
                    symbol = self.config.cash_management.cash_fund
                    primary_exchange = self.config.cash_management.primary_exchange
                    order_exchange = self.config.cash_management.orders.exchange

                    ticker = await self.ibkr.get_ticker_for_stock(
                        symbol, primary_exchange, order_exchange
                    )

                    algo = (
                        self.config.cash_management.orders.algo
                        if self.config.cash_management.orders
                        else self.config.orders.algo
                    )

                    amount = cash_balance + pending_balance - target_cash_balance
                    price = ticker.ask if amount > 0 else ticker.bid
                    qty = amount // price

                    if util.isNan(qty):
                        raise RuntimeError("ERROR: qty is NaN")

                    if qty > 0:
                        log.notice(
                            f"cash_balance={dfmt(cash_balance)} which exceeds "
                            f"(target_cash_balance + pending_balance + buy_threshold)="
                            f"{(dfmt(target_cash_balance + pending_balance + buy_threshold))} "
                            f"with pending_balance={dfmt(pending_balance)}"
                        )
                        log.notice(
                            f"Will buy {symbol} with qty={qty} shares at price={price}"
                        )

                    # make sure qty does not exceed balance if it's a negative value
                    if qty < 0:
                        # subtract 1 to keep cash balance above target
                        qty -= 1
                        log.notice(
                            f"(cash_balance + pending_balance)={dfmt(cash_balance + pending_balance)} which is less than "
                            f"(target_cash_balance + pending_balance - sell_threshold)="
                            f"{(dfmt(target_cash_balance - sell_threshold))} "
                            f"with pending_balance={dfmt(pending_balance)}"
                        )
                        if symbol not in portfolio_positions:
                            # we don't have any positions to sell
                            log.warning(
                                f"Will sell {symbol} with qty={-qty} at"
                                f" price={price}, but we have no position to sell"
                            )
                            return (None, None)
                        positions = [
                            p.position
                            for p in portfolio_positions[symbol]
                            if isinstance(p.contract, Stock)
                        ]
                        position = positions[0] if len(positions) > 0 else 0
                        qty = min([max([-math.floor(position), qty]), 0])
                        # if for some reason the qty is zero, do nothing
                        if qty == 0:
                            log.warning(
                                f"Will sell {symbol} with qty={-qty} at price={price}, but we don't have any shares to sell"
                            )
                            return (None, None)
                        log.notice(
                            f"Will sell {symbol} with qty={-qty} at price={price}"
                        )

                    order = LimitOrder(
                        "BUY" if qty > 0 else "SELL",
                        abs(qty),
                        round(price, 2),
                        algoStrategy=algo.strategy,
                        algoParams=algo_params_from(algo.params),
                        tif="DAY",
                        account=self.account_number,
                        transmit=True,
                    )

                    return (ticker, order)

                if (
                    cash_balance + pending_balance > target_cash_balance + buy_threshold
                    or cash_balance + pending_balance
                    < target_cash_balance - sell_threshold
                ):
                    (ticker, order) = await make_order()
                    if ticker and ticker.contract and order:
                        self.enqueue_order(ticker.contract, order)
                else:
                    log.notice(
                        "All good, nothing to do here. "
                        f"cash_balance={dfmt(cash_balance)} pending_balance={dfmt(pending_balance)}"
                    )

            except RuntimeError:
                log.error("Error occurred when cash hedging. Continuing anyway...")

        await inner_handler()

    def enqueue_order(self, contract: Optional[Contract], order: LimitOrder) -> None:
        if not contract:
            return
        self.orders.add_order(contract, order)

    def submit_orders(self) -> None:
        for contract, order in self.orders.records():
            self.trades.submit_order(contract, order)
        self.trades.print_summary()

    async def adjust_prices(self) -> None:
        if (
            all(
                [
                    not self.config.symbols[symbol].adjust_price_after_delay
                    for symbol in self.config.symbols
                ]
            )
            or self.trades.is_empty()
        ):
            log.warning("Skipping order price adjustments...")
            return

        delay = random.randrange(
            self.config.orders.price_update_delay[0],
            self.config.orders.price_update_delay[1],
        )

        await self.ibkr.wait_for_orders_complete(self.trades.records(), delay)

        unfilled = [
            (idx, trade)
            for idx, trade in enumerate(self.trades.records())
            if trade
            and trade.contract.symbol in self.config.symbols
            and self.config.symbols[trade.contract.symbol].adjust_price_after_delay
            and not trade.isDone()
        ]

        for idx, trade in unfilled:
            try:
                ticker = await self.ibkr.get_ticker_for_contract(
                    trade.contract,
                    required_fields=[TickerField.MIDPOINT],
                    optional_fields=[TickerField.MARKET_PRICE],
                )

                (contract, order) = (trade.contract, trade.order)
                updated_price = np.sign(order.lmtPrice) * max(
                    [
                        (
                            get_minimum_credit(self.config)
                            if order.action == "BUY" and order.lmtPrice <= 0.0
                            else 0.0
                        ),
                        math.fabs(round((order.lmtPrice + ticker.midpoint()) / 2.0, 2)),
                    ]
                )

                # We only want to tighten spreads, not widen them. If the
                # resulting price change would increase the spread, we'll
                # skip it.
                if would_increase_spread(order, updated_price):
                    log.warning(
                        f"Skipping order for {contract.symbol}"
                        f" with old lmtPrice={dfmt(order.lmtPrice)} updated lmtPrice={dfmt(updated_price)}, because updated price would increase spread"
                    )
                    return

                # Check if the updated price is actually any different
                # before proceeding, and make sure the signs match so we
                # don't switch a credit to a debit or vice versa.
                if order.lmtPrice != updated_price and np.sign(
                    order.lmtPrice
                ) == np.sign(updated_price):
                    log.info(
                        f"{contract.symbol}: Resubmitting {order.action} {contract.secType} order with old lmtPrice={dfmt(order.lmtPrice)} updated lmtPrice={dfmt(updated_price)}"
                    )
                    order.lmtPrice = float(updated_price)

                    # For some reason, we need to create a new order object
                    # and populate the fields rather than modifying the
                    # existing order in-place (janky).
                    order = LimitOrder(
                        order.action,
                        order.totalQuantity,
                        updated_price,
                        orderId=order.orderId,
                        algoStrategy=order.algoStrategy,
                        algoParams=order.algoParams,
                    )

                    # resubmit the order and it will be placed back to the
                    # original position in the queue
                    self.trades.submit_order(contract, order, idx)

                    log.info(f"{contract.symbol}: Order updated, order={order}")
            except (RuntimeError, RequiredFieldValidationError):
                log.error(
                    f"Couldn't generate midpoint price for {trade.contract}, skipping"
                )
                continue

    async def get_write_threshold(
        self, ticker: Ticker, right: str
    ) -> tuple[float, float]:
        assert ticker.contract is not None
        absolute_daily_change = math.fabs(ticker.marketPrice() - ticker.close)

        threshold_sigma = get_write_threshold_sigma(
            self.config, ticker.contract.symbol, right
        )
        if threshold_sigma:
            hist_prices = await self.ibkr.request_historical_data(
                ticker.contract, self.config.constants.daily_stddev_window
            )
            log_prices = np.log(np.array([p.close for p in hist_prices]))
            stddev = np.std(np.diff(log_prices), ddof=1)

            return (
                ticker.close * (np.exp(stddev) - 1).astype(float) * threshold_sigma,
                absolute_daily_change,
            )
        else:
            threshold_perc = get_write_threshold_perc(
                self.config, ticker.contract.symbol, right
            )
            return (threshold_perc * ticker.close, absolute_daily_change)
