import math
from functools import lru_cache

import click
from ib_insync import util
from ib_insync.contract import ComboLeg, Contract, Option, Stock, TagValue
from ib_insync.order import LimitOrder

from thetagang.util import (
    account_summary_to_dict,
    count_short_option_positions,
    get_highest_price,
    get_strike_limit,
    get_target_delta,
    midpoint_or_market_price,
    portfolio_positions_to_dict,
    position_pnl,
    wait_n_seconds,
)

from .options import option_dte

# Typically the amount of time needed when waiting on data from the IBKR API.
# Sometimes it can take a while to retrieve data, and it's lazy-loaded by the
# API, so getting this number right is largely a matter of guesswork.
api_response_wait_time = 120


class PortfolioManager:
    def __init__(self, config, ib, completion_future):
        self.orders = []
        self.config = config
        self.ib = ib
        self.completion_future = completion_future
        self.ib.orderStatusEvent += self.orderStatusEvent

    def orderStatusEvent(self, trade):
        if "Filled" in trade.orderStatus.status:
            click.secho(
                f"Order filled, symbol={trade.contract.symbol}",
                fg="green",
            )
        if "Cancelled" in trade.orderStatus.status:
            click.secho(
                f"Order cancelled, symbol={trade.contract.symbol} log={trade.orderStatus.log}",
                fg="red",
            )
        else:
            click.secho(
                f"Order updated, symbol={trade.contract.symbol} status={trade.orderStatus.status}",
                fg="blue",
            )

    def get_calls(self, portfolio_positions):
        return self.get_options(portfolio_positions, "C")

    def get_puts(self, portfolio_positions):
        return self.get_options(portfolio_positions, "P")

    def get_options(self, portfolio_positions, right):
        r = []
        for symbol in portfolio_positions:
            r = r + list(
                filter(
                    lambda p: (
                        isinstance(p.contract, Option)
                        and p.contract.right.startswith(right)
                    ),
                    portfolio_positions[symbol],
                )
            )

        return r

    def wait_for_midpoint_price(self, ticker):
        try:
            wait_n_seconds(
                lambda: util.isNan(ticker.midpoint()),
                lambda: self.ib.waitOnUpdate(timeout=15),
                api_response_wait_time,
            )
        except RuntimeError:
            return False
        return True

    def wait_for_market_price(self, ticker):
        try:
            wait_n_seconds(
                lambda: util.isNan(ticker.marketPrice()),
                lambda: self.ib.waitOnUpdate(timeout=15),
                api_response_wait_time,
            )
        except:
            return False
        return True

    @lru_cache(maxsize=32)
    def get_chains_for_stock(self, stock):
        return self.ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)

    @lru_cache(maxsize=32)
    def get_ticker_for_stock(self, symbol, primary_exchange, midpoint=False):
        stock = Stock(symbol, "SMART", currency="USD", primaryExchange=primary_exchange)
        self.ib.qualifyContracts(stock)
        return self.get_ticker_for(stock)

    @lru_cache(maxsize=32)
    def get_ticker_for(self, contract, midpoint=False):
        [ticker] = self.ib.reqTickers(contract)

        if midpoint:
            self.wait_for_midpoint_price(ticker)
        else:
            self.wait_for_market_price(ticker)

        return ticker

    @lru_cache(maxsize=32)
    def get_ticker_list_for(self, contracts):
        ticker_list = self.ib.reqTickers(*contracts)

        try:
            wait_n_seconds(
                lambda: any([util.isNan(t.midpoint()) for t in ticker_list]),
                lambda: self.ib.waitOnUpdate(timeout=15),
                30,
            )
        except RuntimeError:
            pass

        return ticker_list

    def put_is_itm(self, contract):
        ticker = self.get_ticker_for_stock(contract.symbol, contract.primaryExchange)

        return contract.strike >= ticker.marketPrice()

    def put_can_be_rolled(self, put):
        # Ignore long positions, we only roll shorts
        if put.position > 0:
            return False

        # Check if this put is ITM, and if it's o.k. to roll
        if not self.config["roll_when"]["puts"]["itm"] and self.put_is_itm(
            put.contract
        ):
            return False

        dte = option_dte(put.contract.lastTradeDateOrContractMonth)
        pnl = position_pnl(put)

        roll_when_dte = self.config["roll_when"]["dte"]
        roll_when_pnl = self.config["roll_when"]["pnl"]
        roll_when_min_pnl = self.config["roll_when"]["min_pnl"]

        if (
            "max_dte" in self.config["roll_when"]
            and dte > self.config["roll_when"]["max_dte"]
        ):
            return False

        if dte <= roll_when_dte:
            if pnl >= roll_when_min_pnl:
                click.secho(
                    f"  {put.contract.localSymbol} can be rolled because DTE of {dte} is <= {self.config['roll_when']['dte']} and P&L of {round(pnl * 100, 1)}% is >= {round(roll_when_min_pnl * 100, 1)}%",
                    fg="blue",
                )
                return True
            else:
                click.secho(
                    f"  {put.contract.localSymbol} can't be rolled because P&L of {round(pnl * 100, 1)}% is < {round(roll_when_min_pnl * 100, 1)}%",
                    fg="red",
                )

        if pnl >= roll_when_pnl:
            click.secho(
                f"  {put.contract.localSymbol} can be rolled because P&L of {round(pnl * 100, 1)}% is >= {round(roll_when_pnl * 100, 1)}",
                fg="blue",
            )
            return True

        return False

    def call_is_itm(self, contract):
        ticker = self.get_ticker_for_stock(contract.symbol, contract.primaryExchange)

        return contract.strike <= ticker.marketPrice()

    def call_can_be_rolled(self, call):
        # Ignore long positions, we only roll shorts
        if call.position > 0:
            return False

        # Check if this call is ITM, and it's o.k. to roll
        if not self.config["roll_when"]["calls"]["itm"] and self.call_is_itm(
            call.contract
        ):
            return False

        dte = option_dte(call.contract.lastTradeDateOrContractMonth)
        pnl = position_pnl(call)

        roll_when_dte = self.config["roll_when"]["dte"]
        roll_when_pnl = self.config["roll_when"]["pnl"]
        roll_when_min_pnl = self.config["roll_when"]["min_pnl"]

        if (
            "max_dte" in self.config["roll_when"]
            and dte > self.config["roll_when"]["max_dte"]
        ):
            return False

        if dte <= roll_when_dte:
            if pnl >= roll_when_min_pnl:
                click.secho(
                    f"  {call.contract.localSymbol} can be rolled because DTE of {dte} is <= {self.config['roll_when']['dte']} and P&L of {round(pnl * 100, 1)}% is >= {round(roll_when_min_pnl * 100, 1)}%",
                    fg="blue",
                )
                return True
            else:
                click.secho(
                    f"  {call.contract.localSymbol} can't be rolled because P&L of {round(pnl * 100, 1)}% is < {round(roll_when_min_pnl * 100, 1)}%",
                    fg="red",
                )

        if pnl >= roll_when_pnl:
            click.secho(
                f"  {call.contract.localSymbol} can be rolled because P&L of {round(pnl * 100, 1)}% is >= {round(roll_when_pnl * 100, 1)}",
                fg="blue",
            )
            return True

        return False

    def get_symbols(self):
        return [s for s in self.config["symbols"].keys()]

    def filter_positions(self, portfolio_positions):
        symbols = self.get_symbols()
        return [
            item
            for item in portfolio_positions
            if item.account == self.config["account"]["number"]
            and item.contract.symbol in symbols
            and item.position != 0
            and item.averageCost != 0
        ]

    def get_portfolio_positions(self):
        portfolio_positions = self.ib.portfolio()
        return portfolio_positions_to_dict(self.filter_positions(portfolio_positions))

    def initialize_account(self):
        self.ib.reqMarketDataType(self.config["account"]["market_data_type"])

        if self.config["account"]["cancel_orders"]:
            # Cancel any existing orders
            open_trades = self.ib.openTrades()
            for trade in open_trades:
                if trade.isActive() and trade.contract.symbol in self.get_symbols():
                    click.secho(f"Canceling order {trade.order}", fg="red")
                    self.ib.cancelOrder(trade.order)

    def summarize_account(self):
        account_summary = self.ib.accountSummary(self.config["account"]["number"])
        click.echo()
        click.secho("Account summary:", fg="green")
        click.echo()
        account_summary = account_summary_to_dict(account_summary)

        if "NetLiquidation" not in account_summary:
            raise RuntimeError(
                f"Account number {self.config['account']['number']} appears invalid (no account data returned)"
            )

        justified_values = {
            "ExcessLiquidity": f"{float(account_summary['ExcessLiquidity'].value):,.0f}",
            "NetLiquidation": f"{float(account_summary['NetLiquidation'].value):,.0f}",
            "FullMaintMarginReq": f"{float(account_summary['FullMaintMarginReq'].value):,.0f}",
            "BuyingPower": f"{float(account_summary['BuyingPower'].value):,.0f}",
            "TotalCashValue": f"{float(account_summary['TotalCashValue'].value):,.0f}",
            "Cushion": f"{float(account_summary['Cushion'].value) * 100:.1f}%",
        }

        padding = max([len(v) for v in justified_values.values()])
        justified_values = {k: v.rjust(padding) for k, v in justified_values.items()}

        click.secho(
            f"  Net liquidation   = {justified_values['NetLiquidation']}",
            fg="cyan",
        )
        click.secho(
            f"  Excess liquidity  = {justified_values['ExcessLiquidity']}",
            fg="cyan",
        )
        click.secho(
            f"  Full maint margin = {justified_values['FullMaintMarginReq']}",
            fg="cyan",
        )
        click.secho(
            f"  Buying power      = {justified_values['BuyingPower']}",
            fg="cyan",
        )
        click.secho(
            f"  Total cash value  = {justified_values['TotalCashValue']}",
            fg="cyan",
        )
        click.secho(
            f"  Cushion           = {justified_values['Cushion']}",
            fg="cyan",
        )

        portfolio_positions = self.get_portfolio_positions()

        click.echo()
        click.secho("Portfolio positions:", fg="green")
        click.echo()

        position_values = {}

        def is_itm(p):
            if p.contract.right.startswith("C") and self.call_is_itm(p.contract):
                return "*"
            elif p.contract.right.startswith("P") and self.put_is_itm(p.contract):
                return "*"
            return " "

        for symbol in portfolio_positions.keys():
            for p in portfolio_positions[symbol]:
                position_values[p.contract.conId] = {
                    "qty": str(int(p.position)),
                    "mktprice": f"{p.marketPrice:,.2f}",
                    "avgprice": f"{p.averageCost:,.2f}",
                    "value": f"{p.marketValue:,.0f}",
                    "cost": f"{(p.averageCost * p.position):,.0f}",
                    "p&l": f"{(position_pnl(p) * 100):.2f}%",
                    "itm?": is_itm(p),
                }
                if isinstance(p.contract, Option):
                    position_values[p.contract.conId][
                        "avgprice"
                    ] = f"{p.averageCost/float(p.contract.multiplier):,.2f}"
                    position_values[p.contract.conId][
                        "strike"
                    ] = f"{float(p.contract.strike):,.2f}"
                    position_values[p.contract.conId]["dte"] = str(
                        option_dte(p.contract.lastTradeDateOrContractMonth)
                    )
                    position_values[p.contract.conId]["exp"] = str(
                        p.contract.lastTradeDateOrContractMonth
                    )
        padding = {
            "qty": len("Qty"),
            "mktprice": len("MktPrice"),
            "avgprice": len("AvgPrice"),
            "value": len("Value"),
            "cost": len("Cost"),
            "p&l": len("P&L"),
            "strike": len("Strike"),
            "dte": len("DTE"),
            "exp": len("Exp"),
            "itm?": len("ITM?"),
        }
        for _id, p in position_values.items():
            for col, value in p.items():
                padding[col] = max(padding[col], len(value))

        # Print column headers
        def print_col(c):
            return c.rjust(padding[c.lower()])

        click.secho(
            f"           {print_col('Qty')}  {print_col('MktPrice')}  {print_col('AvgPrice')}  {print_col('Value')}  {print_col('Cost')}  {print_col('P&L')}  {print_col('Strike')}  {print_col('DTE')}  {print_col('Exp')}  {print_col('ITM?')}",
            fg="green",
        )

        for symbol in portfolio_positions.keys():
            click.secho(f"  {symbol}:", fg="cyan")
            sorted_positions = sorted(
                portfolio_positions[symbol],
                key=lambda p: option_dte(p.contract.lastTradeDateOrContractMonth)
                if isinstance(p.contract, Option)
                else -1,  # Keep stonks on top
            )

            def pad(col, id):
                return position_values[id][col].rjust(padding[col])

            for p in sorted_positions:
                id = p.contract.conId
                qty = pad("qty", id)
                mktPrice = pad("mktprice", id)
                avgPrice = pad("avgprice", id)
                value = pad("value", id)
                cost = pad("cost", id)
                pnl = pad("p&l", id)
                if isinstance(p.contract, Stock):
                    click.secho(
                        f"    Stock  {qty}  {mktPrice}  {avgPrice}  {value}  {cost}  {pnl}",
                        fg="cyan",
                    )
                elif isinstance(p.contract, Option):
                    strike = pad("strike", id)
                    dte = pad("dte", id)
                    exp = pad("exp", id)
                    itm = pad("itm?", id)

                    def p_or_c(p):
                        return "Call" if p.contract.right.startswith("C") else "Put "

                    click.secho(
                        f"    {p_or_c(p)}   {qty}  {mktPrice}  {avgPrice}  {value}  {cost}  {pnl}  {strike}  {dte}  {exp}  {itm}",
                        fg="cyan",
                    )
                else:
                    click.secho(f"    {p.contract}", fg="cyan")

        return (account_summary, portfolio_positions)

    def manage(self):
        try:
            self.initialize_account()
            (account_summary, portfolio_positions) = self.summarize_account()

            click.echo()
            click.secho("Checking positions...", fg="green")

            self.check_puts(account_summary, portfolio_positions)
            self.check_calls(account_summary, portfolio_positions)

            # Look for lots of stock that don't have covered calls
            self.check_for_uncovered_positions(account_summary, portfolio_positions)

            # Refresh positions, in case anything changed from the ordering above
            portfolio_positions = self.get_portfolio_positions()

            # Check if we have enough buying power to write some puts
            self.check_if_can_write_puts(account_summary, portfolio_positions)

            # Wait for pending orders
            wait_n_seconds(
                lambda: any(
                    "Pending" in trade.orderStatus.status for trade in self.orders
                ),
                lambda: self.ib.waitOnUpdate(timeout=15),
                api_response_wait_time,
            )

            click.echo()
            click.secho("ThetaGang is done, shutting down! Cya next time.", fg="yellow")

        except:
            click.secho("An exception was raised, exiting", fg="red")
            click.secho("Check log for details", fg="red")
            raise

        finally:
            # Shut it down
            self.completion_future.set_result(True)

    def check_puts(self, account_summary, portfolio_positions):
        # Check for puts which may be rolled to the next expiration or a better price
        puts = self.get_puts(portfolio_positions)

        # find puts eligible to be rolled
        rollable_puts = list(filter(lambda p: self.put_can_be_rolled(p), puts))

        total_rollable_puts = math.floor(sum([abs(p.position) for p in rollable_puts]))

        click.echo()
        click.secho(f"{total_rollable_puts} puts can be rolled", fg="magenta")

        self.roll_puts(rollable_puts, account_summary)

    def check_calls(self, account_summary, portfolio_positions):
        # Check for calls which may be rolled to the next expiration or a better price
        calls = self.get_calls(portfolio_positions)

        # find calls eligible to be rolled
        rollable_calls = list(filter(lambda p: self.call_can_be_rolled(p), calls))
        total_rollable_calls = math.floor(
            sum([abs(p.position) for p in rollable_calls])
        )

        click.echo()
        click.secho(f"{total_rollable_calls} calls can be rolled", fg="magenta")

        self.roll_calls(rollable_calls, account_summary, portfolio_positions)

    def get_maximum_new_contracts_for(self, symbol, primary_exchange, account_summary):
        total_buying_power = self.get_buying_power(account_summary)
        max_buying_power = (
            self.config["target"]["maximum_new_contracts_percent"] * total_buying_power
        )
        ticker = self.get_ticker_for_stock(
            symbol,
            primary_exchange,
        )

        return max([1, round((max_buying_power / ticker.marketPrice()) // 100)])

    def check_for_uncovered_positions(self, account_summary, portfolio_positions):
        for symbol in portfolio_positions:
            call_count = max(
                [0, count_short_option_positions(symbol, portfolio_positions, "C")]
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
                    [get_strike_limit(self.config, symbol, "C") or 0]
                    + [
                        p.averageCost
                        for p in portfolio_positions[symbol]
                        if isinstance(p.contract, Stock)
                    ]
                )
            )

            target_calls = max([0, stock_count // 100])
            new_contracts_needed = target_calls - call_count

            maximum_new_contracts = self.get_maximum_new_contracts_for(
                symbol,
                self.get_primary_exchange(symbol),
                account_summary,
            )
            calls_to_write = max(
                [0, min([new_contracts_needed, maximum_new_contracts])]
            )

            if calls_to_write > 0:
                click.secho(
                    f"Will write {calls_to_write} calls, {new_contracts_needed} needed for {symbol}, capped at {maximum_new_contracts}, at or above strike ${strike_limit} (target_calls={target_calls}, call_count={call_count})",
                    fg="green",
                )
                self.write_calls(
                    symbol,
                    self.get_primary_exchange(symbol),
                    calls_to_write,
                    strike_limit,
                )

    def write_calls(self, symbol, primary_exchange, quantity, strike_limit):
        sell_ticker = self.find_eligible_contracts(
            symbol, primary_exchange, "C", strike_limit
        )

        if not self.wait_for_midpoint_price(sell_ticker):
            click.secho(
                "Couldn't get midpoint price for contract={sell_ticker}, skipping for now",
                fg="red",
            )
            return

        # Create order
        order = LimitOrder(
            "SELL",
            quantity,
            round(get_highest_price(sell_ticker), 2),
            algoStrategy="Adaptive",
            algoParams=[TagValue("adaptivePriority", "Patient")],
            tif="DAY",
        )

        # Submit order
        try:
            trade = self.ib.placeOrder(sell_ticker.contract, order)
            self.orders.append(trade)
            click.echo()
            click.secho("Order submitted", fg="green")
            click.secho(f"{trade}", fg="green")
        except RuntimeError as e:
            click.echo()
            click.secho(e, fg="red")
            click.secho(
                "Order trade submission seems to have failed, or a response wasn't received in time. Continuing anyway...",
                fg="yellow",
            )

    def write_puts(self, symbol, primary_exchange, quantity, strike_limit):
        try:
            sell_ticker = self.find_eligible_contracts(
                symbol, primary_exchange, "P", strike_limit
            )
        except RuntimeError as e:
            click.echo()
            click.secho(e, fg="red")
            click.secho(
                f"Finding eligible contracts for {symbol} failed. Continuing anyway...",
                fg="yellow",
            )
            return

        if not self.wait_for_midpoint_price(sell_ticker):
            click.secho(
                "Couldn't get midpoint price for contract={sell_ticker}, skipping for now",
                fg="red",
            )
            return

        # Create order
        order = LimitOrder(
            "SELL",
            quantity,
            round(get_highest_price(sell_ticker), 2),
            algoStrategy="Adaptive",
            algoParams=[TagValue("adaptivePriority", "Patient")],
            tif="DAY",
        )

        # Submit order
        trade = self.ib.placeOrder(sell_ticker.contract, order)
        self.orders.append(trade)
        click.echo()
        click.secho("Order submitted", fg="green")
        click.secho(f"{trade}", fg="green")

    def get_primary_exchange(self, symbol):
        return self.config["symbols"][symbol].get("primary_exchange", "")

    def get_buying_power(self, account_summary):
        return math.floor(
            float(account_summary["NetLiquidation"].value)
            * self.config["account"]["margin_usage"]
        )

    def check_if_can_write_puts(self, account_summary, portfolio_positions):
        # Get stock positions
        stock_positions = [
            position
            for symbol in portfolio_positions
            for position in portfolio_positions[symbol]
            if isinstance(position.contract, Stock)
        ]

        total_buying_power = self.get_buying_power(account_summary)

        click.echo()
        click.secho(
            f"Total buying power: ${total_buying_power:,.0f} at {round(self.config['account']['margin_usage'] * 100, 1)}% margin usage",
            fg="green",
        )
        click.echo()

        stock_symbols = dict()
        for stock in stock_positions:
            symbol = stock.contract.symbol
            stock_symbols[symbol] = stock

        targets = dict()
        target_additional_quantity = dict()

        # Determine target quantity of each stock
        for symbol in self.config["symbols"].keys():
            click.secho(f"  {symbol}", fg="green")

            ticker = self.get_ticker_for_stock(
                symbol, self.get_primary_exchange(symbol)
            )

            current_position = math.floor(
                stock_symbols[symbol].position if symbol in stock_symbols else 0
            )
            click.secho(
                f"    Current position quantity: {current_position} shares",
                fg="cyan",
            )

            targets[symbol] = round(
                self.config["symbols"][symbol]["weight"] * total_buying_power, 2
            )
            click.secho(f"    Target value: ${targets[symbol]:,.0f}", fg="cyan")
            target_quantity = math.floor(targets[symbol] / ticker.marketPrice())
            click.secho(f"    Target share quantity: {target_quantity:,d}", fg="cyan")

            # Current number of short puts
            put_count = count_short_option_positions(symbol, portfolio_positions, "P")

            target_additional_quantity[symbol] = math.floor(
                target_quantity - current_position - 100 * put_count
            )

            click.secho(
                f"    Net quantity: {target_additional_quantity[symbol]:,d} shares, {target_additional_quantity[symbol] // 100} contracts",
                fg="cyan",
            )

        click.echo()

        # Figure out how many additional puts are needed, if they're needed
        for symbol in target_additional_quantity.keys():
            additional_quantity = target_additional_quantity[symbol] // 100
            # NOTE: it's possible there are non-standard option contract sizes,
            # like with futures, but we don't bother handling those cases.
            # Please don't use this code with futures.
            if additional_quantity >= 1:
                maximum_new_contracts = self.get_maximum_new_contracts_for(
                    symbol,
                    self.get_primary_exchange(symbol),
                    account_summary,
                )
                puts_to_write = min([additional_quantity, maximum_new_contracts])
                if puts_to_write > 0:
                    strike_limit = get_strike_limit(self.config, symbol, "P")
                    if strike_limit:
                        click.secho(
                            f"Will write {puts_to_write} puts, {additional_quantity} needed for {symbol}, capped at {maximum_new_contracts}, at or below strike ${strike_limit}",
                            fg="cyan",
                        )
                    else:
                        click.secho(
                            f"Will write {puts_to_write} puts, {additional_quantity} needed for {symbol}, capped at {maximum_new_contracts}",
                            fg="cyan",
                        )
                    self.write_puts(
                        symbol,
                        self.get_primary_exchange(symbol),
                        puts_to_write,
                        strike_limit,
                    )

        return

    def roll_puts(self, puts, account_summary):
        return self.roll_positions(puts, "P", account_summary)

    def roll_calls(self, calls, account_summary, portfolio_positions):
        return self.roll_positions(calls, "C", account_summary, portfolio_positions)

    def roll_positions(self, positions, right, account_summary, portfolio_positions={}):
        for position in positions:
            symbol = position.contract.symbol
            strike_limit = get_strike_limit(self.config, symbol, right)
            if right.startswith("C"):
                strike_limit = math.ceil(
                    max(
                        [strike_limit or 0]
                        + [
                            p.averageCost
                            for p in portfolio_positions[symbol]
                            if isinstance(p.contract, Stock)
                        ]
                    )
                )

            sell_ticker = self.find_eligible_contracts(
                symbol,
                self.get_primary_exchange(symbol),
                right,
                strike_limit,
                excluded_expiration=position.contract.lastTradeDateOrContractMonth,
                excluded_strikes=[position.contract.strike]
            )

            quantity = abs(position.position)
            maximum_new_contracts = self.get_maximum_new_contracts_for(
                symbol,
                self.get_primary_exchange(symbol),
                account_summary,
            )
            dte = option_dte(position.contract.lastTradeDateOrContractMonth)
            roll_when_dte = self.config["roll_when"]["dte"]
            if dte > roll_when_dte:
                quantity = min([quantity, maximum_new_contracts])

            position.contract.exchange = "SMART"
            buy_ticker = self.get_ticker_for(position.contract, midpoint=True)

            price = midpoint_or_market_price(buy_ticker) - midpoint_or_market_price(
                sell_ticker
            )

            # Create combo legs
            comboLegs = [
                ComboLeg(
                    conId=position.contract.conId,
                    ratio=1,
                    exchange="SMART",
                    action="BUY",
                ),
                ComboLeg(
                    conId=sell_ticker.contract.conId,
                    ratio=1,
                    exchange="SMART",
                    action="SELL",
                ),
            ]

            # Create contract
            combo = Contract(
                secType="BAG",
                symbol=symbol,
                currency="USD",
                exchange="SMART",
                comboLegs=comboLegs,
            )

            # Create order
            order = LimitOrder(
                "BUY",
                quantity,
                round(price, 2),
                algoStrategy="Adaptive",
                algoParams=[TagValue("adaptivePriority", "Patient")],
                tif="DAY",
            )

            # Submit order
            trade = self.ib.placeOrder(combo, order)
            self.orders.append(trade)
            click.echo()
            click.secho(
                f"Order submitted, current position={abs(position.position)} quantity to roll={quantity}, dte={dte}, price={round(price,2)}, trade={trade}",
                fg="green",
            )

    def find_eligible_contracts(
        self, symbol, primary_exchange, right, strike_limit, excluded_expiration=None, excluded_strikes=[]
    ):
        click.echo()
        click.secho(
            f"Searching option chain for symbol={symbol} right={right}, this can take a while...",
            fg="green",
        )
        click.echo()
        stock = Stock(symbol, "SMART", currency="USD", primaryExchange=primary_exchange)
        self.ib.qualifyContracts(stock)

        ticker = self.get_ticker_for(stock)
        tickerValue = ticker.marketPrice()

        chains = self.get_chains_for_stock(stock)
        chain = next(c for c in chains if c.exchange == "SMART")

        def valid_strike(strike):
            if strike in excluded_strikes:
                return False
            elif right.startswith("P") and strike_limit:
                return strike <= tickerValue and strike <= strike_limit
            elif right.startswith("P"):
                return strike <= tickerValue
            elif right.startswith("C") and strike_limit:
                return strike >= tickerValue and strike >= strike_limit
            elif right.startswith("C"):
                return strike >= tickerValue
            return False

        chain_expirations = self.config["option_chains"]["expirations"]
        min_dte = option_dte(excluded_expiration) if excluded_expiration else 0

        strikes = sorted(strike for strike in chain.strikes if valid_strike(strike))
        expirations = sorted(
            exp
            for exp in chain.expirations
            if option_dte(exp) >= self.config["target"]["dte"]
            and option_dte(exp) >= min_dte
        )[:chain_expirations]
        rights = [right]

        def nearest_strikes(strikes):
            chain_strikes = self.config["option_chains"]["strikes"]
            if right.startswith("P"):
                return strikes[-chain_strikes:]
            if right.startswith("C"):
                return strikes[:chain_strikes]

        contracts = [
            Option(
                symbol,
                expiration,
                strike,
                right,
                "SMART",
                tradingClass=chain.tradingClass,
            )
            for right in rights
            for expiration in expirations
            for strike in nearest_strikes(strikes)
        ]

        contracts = self.ib.qualifyContracts(*contracts)

        tickers = self.get_ticker_list_for(tuple(contracts))

        # Filter out contracts which don't have a midpoint price
        tickers = [t for t in tickers if not util.isNan(t.midpoint())]

        def open_interest_is_valid(ticker):
            def open_interest_is_not_ready():
                if right.startswith("P"):
                    return util.isNan(ticker.putOpenInterest)
                if right.startswith("C"):
                    return util.isNan(ticker.callOpenInterest)

            try:
                wait_n_seconds(
                    open_interest_is_not_ready,
                    lambda: self.ib.waitOnUpdate(timeout=15),
                    api_response_wait_time,
                )
            except RuntimeError:
                click.secho(
                    f"Timeout waiting on market data for {ticker.contract}. Continuing...",
                    fg="yellow",
                )
                return False
            finally:
                self.ib.cancelMktData(ticker.contract)

            # The open interest value is never present when using historical
            # data, so just ignore it when the value is None
            if right.startswith("P"):
                return (
                    ticker.putOpenInterest
                    >= self.config["target"]["minimum_open_interest"]
                )
            if right.startswith("C"):
                return (
                    ticker.callOpenInterest
                    >= self.config["target"]["minimum_open_interest"]
                )

        def delta_is_valid(ticker):
            return (
                ticker.modelGreeks
                and not util.isNan(ticker.modelGreeks.delta)
                and ticker.modelGreeks.delta is not None
                and abs(ticker.modelGreeks.delta)
                <= get_target_delta(self.config, symbol, right)
            )

        def price_is_valid(ticker):
            return not util.isNan(ticker.midpoint()) or not util.isNan(
                ticker.marketPrice()
            )

        # Filter out tickers without prices
        tickers = [ticker for ticker in tickers if price_is_valid(ticker)]
        # Filter by delta and open interest
        tickers = [ticker for ticker in tickers if delta_is_valid(ticker)]
        # Fetch market data
        tickers = [
            self.ib.reqMktData(ticker.contract, genericTickList="101")
            for ticker in tickers
        ]
        # Fetch open interest
        tickers = [ticker for ticker in tickers if open_interest_is_valid(ticker)]
        # Sort by delta first, then expiry date
        tickers = sorted(
            reversed(sorted(tickers, key=lambda t: abs(t.modelGreeks.delta))),
            key=lambda t: option_dte(t.contract.lastTradeDateOrContractMonth),
        )

        if len(tickers) == 0:
            raise RuntimeError(f"No valid contracts found for {symbol}. Aborting.")

        # Return the first result
        return tickers[0]
