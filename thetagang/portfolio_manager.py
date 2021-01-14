import math

import click
from ib_insync import util
from ib_insync.contract import ComboLeg, Contract, Option, Stock, TagValue
from ib_insync.order import LimitOrder, Order

from thetagang.util import (
    account_summary_to_dict,
    count_option_positions,
    justify,
    midpoint_or_market_price,
    portfolio_positions_to_dict,
    position_pnl,
    while_n_times,
)

from .options import option_dte


class PortfolioManager:
    def __init__(self, config, ib, completion_future):
        self.config = config
        self.ib = ib
        self.completion_future = completion_future

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
        while_n_times(
            lambda: util.isNan(ticker.midpoint()),
            lambda: self.ib.waitOnUpdate(timeout=2),
            10,
        )

    def wait_for_market_price(self, ticker):
        while_n_times(
            lambda: util.isNan(ticker.marketPrice()),
            lambda: self.ib.waitOnUpdate(timeout=2),
            10,
        )

    def put_is_itm(self, contract):
        stock = Stock(contract.symbol, "SMART", currency="USD")
        [ticker] = self.ib.reqTickers(stock)

        self.wait_for_market_price(ticker)

        return contract.strike >= ticker.marketPrice()

    def put_can_be_rolled(self, put):
        # Check if this put is ITM, and if it's o.k. to roll
        if (
            "puts" not in self.config["roll_when"]
            or not self.config["roll_when"]["puts"]["itm"]
        ) and self.put_is_itm(put.contract):
            return False

        dte = option_dte(put.contract.lastTradeDateOrContractMonth)
        pnl = position_pnl(put)

        if dte <= self.config["roll_when"]["dte"]:
            click.secho(
                f"  {put.contract.localSymbol} can be rolled because DTE of {dte} is <= {self.config['roll_when']['dte']}",
                fg="blue",
            )
            return True

        if pnl >= self.config["roll_when"]["pnl"]:
            click.secho(
                f"  {put.contract.localSymbol} can be rolled because P&L of {round(pnl * 100, 1)}% is >= {round(self.config['roll_when']['pnl'] * 100, 1)}",
                fg="blue",
            )
            return True

        return False

    def call_is_itm(self, contract):
        stock = Stock(contract.symbol, "SMART", currency="USD")
        [ticker] = self.ib.reqTickers(stock)

        self.wait_for_market_price(ticker)

        return contract.strike <= ticker.marketPrice()

    def call_can_be_rolled(self, call):
        # Check if this call is ITM, and it's o.k. to roll
        if (
            "calls" in self.config["roll_when"]
            and not self.config["roll_when"]["calls"]["itm"]
        ) and self.call_is_itm(call.contract):
            return False

        dte = option_dte(call.contract.lastTradeDateOrContractMonth)
        pnl = position_pnl(call)

        if dte <= self.config["roll_when"]["dte"]:
            click.secho(
                f"{call.contract.localSymbol} can be rolled because DTE of {dte} is <= {self.config['roll_when']['dte']}",
                fg="blue",
            )
            return True

        if pnl >= self.config["roll_when"]["pnl"]:
            click.secho(
                f"{call.contract.localSymbol} can be rolled because P&L of {round(pnl * 100, 1)}% is >= {round(self.config['roll_when']['pnl'] * 100, 1)}",
                fg="blue",
            )
            return True

        return False

    def filter_positions(self, portfolio_positions):
        keys = portfolio_positions.keys()
        for k in keys:
            if k not in self.config["symbols"]:
                del portfolio_positions[k]
        return portfolio_positions

    def get_portfolio_positions(self):
        portfolio_positions = self.ib.portfolio()
        # Filter out any positions we don't care about, i.e., we don't know the
        # symbol or it's not in the desired account.
        portfolio_positions = [
            item
            for item in portfolio_positions
            if item.account == self.config["account"]["number"]
            and item.contract.symbol in self.config["symbols"]
        ]
        return portfolio_positions_to_dict(portfolio_positions)

    def initialize_account(self):
        self.ib.reqMarketDataType(self.config["account"]["market_data_type"])

        if self.config["account"]["cancel_orders"]:
            # Cancel any existing orders
            open_trades = self.ib.openTrades()
            for trade in open_trades:
                if trade.isActive() and trade.contract.symbol in self.config["symbols"]:
                    click.secho(f"Canceling order {trade.order}", fg="red")
                    self.ib.cancelOrder(trade.order)

    def summarize_account(self):
        account_summary = self.ib.accountSummary(self.config["account"]["number"])
        click.echo()
        click.secho(f"Account summary:", fg="green")
        click.echo()
        account_summary = account_summary_to_dict(account_summary)

        click.secho(
            f"  Excess liquidity  = {justify(account_summary['ExcessLiquidity'].value)}",
            fg="cyan",
        )
        click.secho(
            f"  Net liquidation   = {justify(account_summary['NetLiquidation'].value)}",
            fg="cyan",
        )
        click.secho(
            f"  Cushion           = {account_summary['Cushion'].value} ({round(float(account_summary['Cushion'].value) * 100, 1)}%)",
            fg="cyan",
        )
        click.secho(
            f"  Full maint margin = {justify(account_summary['FullMaintMarginReq'].value)}",
            fg="cyan",
        )
        click.secho(
            f"  Buying power      = {justify(account_summary['BuyingPower'].value)}",
            fg="cyan",
        )
        click.secho(
            f"  Total cash value  = {justify(account_summary['TotalCashValue'].value)}",
            fg="cyan",
        )

        portfolio_positions = self.get_portfolio_positions()

        click.echo()
        click.secho("Portfolio positions:", fg="green")
        click.echo()
        for symbol in portfolio_positions.keys():
            click.secho(f"  {symbol}:", fg="cyan")
            for p in portfolio_positions[symbol]:
                if isinstance(p.contract, Stock):
                    pnl = round(position_pnl(p) * 100, 2)
                    click.secho(
                        f"    Stock Qty={int(p.position)} Price={round(p.marketPrice, 2)} Value={round(p.marketValue,2)} Cost={round(p.averageCost * p.position,2)} P&L={pnl}%",
                        fg="cyan",
                    )
                elif isinstance(p.contract, Option):
                    pnl = round(position_pnl(p) * 100, 2)

                    def p_or_c(p):
                        return "Call" if p.contract.right.startswith("C") else "Put "

                    click.secho(
                        f"    {p_or_c(p)}  Qty={int(p.position)} Price={round(p.marketPrice, 2)} Value={round(p.marketValue,2)} Cost={round(p.averageCost * p.position,2)} P&L={pnl}% Strike={p.contract.strike} Exp={p.contract.lastTradeDateOrContractMonth}",
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
            click.echo()

            portfolio_positions = self.filter_positions(portfolio_positions)

            self.check_puts(portfolio_positions)
            self.check_calls(portfolio_positions)

            # Look for lots of stock that don't have covered calls
            self.check_for_uncovered_positions(portfolio_positions)

            # Refresh positions, in case anything changed from the ordering above
            portfolio_positions = self.get_portfolio_positions()

            # Check if we have enough buying power to write some puts
            self.check_if_can_write_puts(account_summary, portfolio_positions)

            click.echo()
            click.secho("ThetaGang is done, shutting down! Cya next time.", fg="yellow")
            click.echo()

        except:
            click.secho("An exception was raised, exiting", fg="red")
            click.secho("Check log for details", fg="red")
            raise

        finally:
            # Shut it down
            self.completion_future.set_result(True)

    def check_puts(self, portfolio_positions):
        # Check for puts which may be rolled to the next expiration or a better price
        puts = self.get_puts(portfolio_positions)

        # find puts eligible to be rolled
        rollable_puts = list(filter(lambda p: self.put_can_be_rolled(p), puts))

        total_rollable_puts = math.floor(sum([abs(p.position) for p in rollable_puts]))

        click.echo()
        click.secho(f"{total_rollable_puts} puts will be rolled", fg="magenta")
        click.echo()

        self.roll_puts(rollable_puts)

    def check_calls(self, portfolio_positions):
        # Check for calls which may be rolled to the next expiration or a better price
        calls = self.get_calls(portfolio_positions)

        # find calls eligible to be rolled
        rollable_calls = list(filter(lambda p: self.call_can_be_rolled(p), calls))
        total_rollable_calls = math.floor(
            sum([abs(p.position) for p in rollable_calls])
        )

        click.echo()
        click.secho(f"{total_rollable_calls} calls will be rolled", fg="magenta")
        click.echo()

        self.roll_calls(rollable_calls)

    def check_for_uncovered_positions(self, portfolio_positions):
        for symbol in portfolio_positions:
            call_count = count_option_positions(symbol, portfolio_positions, "C")
            stock_count = math.floor(
                sum(
                    [
                        p.position
                        for p in portfolio_positions[symbol]
                        if isinstance(p.contract, Stock)
                    ]
                )
            )

            target_calls = stock_count // 100

            calls_to_write = target_calls - call_count

            if calls_to_write > 0:
                click.secho(f"Need to write {calls_to_write} for {symbol}", fg="green")
                self.write_calls(symbol, calls_to_write)

    def wait_for_trade_submitted(self, trade):
        while_n_times(
            lambda: trade.orderStatus.status
            not in [
                "Submitted",
                "Filled",
                "ApiCancelled",
                "Cancelled",
            ],
            lambda: self.ib.waitOnUpdate(timeout=2),
            10,
        )
        return trade

    def write_calls(self, symbol, quantity):
        sell_ticker = self.find_eligible_contracts(symbol, "C")

        self.wait_for_midpoint_price(sell_ticker)

        # Create order
        order = LimitOrder(
            "SELL",
            quantity,
            round(midpoint_or_market_price(sell_ticker), 2),
            algoStrategy="Adaptive",
            algoParams=[TagValue("adaptivePriority", "Patient")],
            tif="DAY",
        )

        # Submit order
        trade = self.wait_for_trade_submitted(
            self.ib.placeOrder(sell_ticker.contract, order)
        )
        click.echo()
        click.secho("Order submitted", fg="green")
        click.secho(f"{trade}", fg="green")

    def write_puts(self, symbol, quantity):
        sell_ticker = self.find_eligible_contracts(symbol, "P")

        self.wait_for_midpoint_price(sell_ticker)

        # Create order
        order = LimitOrder(
            "SELL",
            quantity,
            round(midpoint_or_market_price(sell_ticker), 2),
            algoStrategy="Adaptive",
            algoParams=[TagValue("adaptivePriority", "Patient")],
            tif="DAY",
        )

        # Submit order
        trade = self.wait_for_trade_submitted(
            self.ib.placeOrder(sell_ticker.contract, order)
        )
        click.echo()
        click.secho("Order submitted", fg="green")
        click.secho(f"{trade}", fg="green")

    def check_if_can_write_puts(self, account_summary, portfolio_positions):
        # Get stock positions
        stock_positions = [
            position
            for symbol in portfolio_positions
            for position in portfolio_positions[symbol]
            if isinstance(position.contract, Stock)
        ]

        total_buying_power = math.floor(
            float(account_summary["NetLiquidation"].value)
            * self.config["account"]["margin_usage"]
        )

        click.echo()
        click.secho(
            f"Total buying power: ${total_buying_power} at {round(self.config['account']['margin_usage'] * 100, 1)}% margin usage",
            fg="green",
        )

        # Sum stock values that we care about
        total_value = (
            sum([stock.marketValue for stock in stock_positions]) + total_buying_power
        )
        click.secho(f"Total value: ${total_value}", fg="green")
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
            stock = Stock(symbol, "SMART", currency="USD")
            [ticker] = self.ib.reqTickers(stock)

            self.wait_for_market_price(ticker)

            current_position = math.floor(
                stock_symbols[symbol].position if symbol in stock_symbols else 0
            )
            click.secho(
                f"    Current position quantity {current_position}",
                fg="cyan",
            )

            targets[symbol] = round(
                self.config["symbols"][symbol]["weight"] * total_value, 2
            )
            click.secho(f"    Target value ${targets[symbol]}", fg="cyan")
            target_quantity = math.floor(targets[symbol] / ticker.marketPrice())
            click.secho(f"    Target quantity {target_quantity}", fg="cyan")

            target_additional_quantity[symbol] = math.floor(
                target_quantity - current_position
            )

            click.secho(
                f"    Target additional quantity (excl. existing options) {target_additional_quantity[symbol]}",
                fg="cyan",
            )

        click.echo()

        # Figure out how many addition puts are needed, if they're needed
        for symbol in target_additional_quantity.keys():
            additional_quantity = target_additional_quantity[symbol]
            # NOTE: it's possible there are non-standard option contract sizes,
            # like with futures, but we don't bother handling those cases.
            # Please don't use this code with futures.
            if additional_quantity >= 100:
                put_count = count_option_positions(symbol, portfolio_positions, "P")
                target_put_count = additional_quantity // 100
                puts_to_write = target_put_count - put_count
                if puts_to_write > 0:
                    click.secho(
                        f"Preparing to write additional {puts_to_write} puts to purchase {symbol}",
                        fg="cyan",
                    )
                    self.write_puts(symbol, puts_to_write)

        return

    def roll_puts(self, puts):
        return self.roll_positions(puts, "P")

    def roll_calls(self, calls):
        return self.roll_positions(calls, "C")

    def roll_positions(self, positions, right):
        for position in positions:
            symbol = position.contract.symbol

            sell_ticker = self.find_eligible_contracts(symbol, right)
            self.wait_for_midpoint_price(sell_ticker)

            quantity = abs(position.position)

            position.contract.exchange = "SMART"
            [buy_ticker] = self.ib.reqTickers(position.contract)
            self.wait_for_midpoint_price(buy_ticker)

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
            trade = self.wait_for_trade_submitted(self.ib.placeOrder(combo, order))
            click.secho("Order submitted", fg="green")
            click.secho(f"{trade}", fg="green")

    def find_eligible_contracts(self, symbol, right):
        click.echo()
        click.secho(
            f"Searching option chain for symbol={symbol} right={right}, this can take a while...",
            fg="green",
        )
        click.echo()
        stock = Stock(symbol, "SMART", currency="USD")
        contracts = self.ib.qualifyContracts(stock)

        [ticker] = self.ib.reqTickers(stock)
        tickerValue = ticker.marketPrice()

        chains = self.ib.reqSecDefOptParams(
            stock.symbol, "", stock.secType, stock.conId
        )
        chain = next(c for c in chains if c.exchange == "SMART")

        def valid_strike(strike):
            if right.startswith("P"):
                return strike <= tickerValue
            if right.startswith("C"):
                return strike >= tickerValue
            return False

        chain_expirations = self.config["option_chains"]["expirations"]

        strikes = sorted(strike for strike in chain.strikes if valid_strike(strike))
        expirations = sorted(
            exp
            for exp in chain.expirations
            if option_dte(exp) >= self.config["target"]["dte"]
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

        tickers = self.ib.reqTickers(*contracts)

        def open_interest_is_valid(ticker):
            ticker = self.ib.reqMktData(ticker.contract, genericTickList="101")

            def open_interest_is_not_ready():
                if right.startswith("P"):
                    return util.isNan(ticker.putOpenInterest)
                if right.startswith("C"):
                    return util.isNan(ticker.callOpenInterest)

            while_n_times(
                open_interest_is_not_ready, lambda: self.ib.waitOnUpdate(timeout=2), 10
            )
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
                and ticker.modelGreeks.delta
                and abs(ticker.modelGreeks.delta) <= self.config["target"]["delta"]
            )

        # Filter by delta and open interest
        tickers = [ticker for ticker in tickers if delta_is_valid(ticker)]
        tickers = [ticker for ticker in tickers if open_interest_is_valid(ticker)]
        tickers = sorted(
            reversed(sorted(tickers, key=lambda t: abs(t.modelGreeks.delta))),
            key=lambda t: option_dte(t.contract.lastTradeDateOrContractMonth),
        )

        if len(tickers) == 0:
            raise RuntimeError(f"No valid contracts found for {symbol}. Aborting.")

        # Return the first result
        return tickers[0]
