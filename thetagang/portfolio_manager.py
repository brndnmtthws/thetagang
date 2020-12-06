import math

import click
import ib_insync
from ib_insync.contract import ComboLeg, Contract, Option, Stock, TagValue
from ib_insync.order import LimitOrder, Order

from thetagang.util import position_pnl

from .options import contract_date_to_datetime, option_dte


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

    def put_is_itm(self, contract):
        stock = Stock(contract.symbol, "SMART", currency="USD")
        [ticker] = self.ib.reqTickers(stock)
        return contract.strike >= ticker.marketPrice()

    def put_can_be_rolled(self, put):
        # Check if this put is ITM. Do not roll ITM puts.
        if self.put_is_itm(put.contract):
            return False

        dte = option_dte(put.contract.lastTradeDateOrContractMonth)
        pnl = position_pnl(put)

        if dte <= self.config["roll_when"]["dte"]:
            click.secho(
                f"{put.contract.localSymbol} can be rolled because DTE of {dte} is <= {self.config['roll_when']['dte']}",
                fg="blue",
            )
            return True

        if pnl >= self.config["roll_when"]["pnl"]:
            click.secho(
                f"{put.contract.localSymbol} can be rolled because P&L of {round(pnl * 100, 1)}% is >= {round(self.config['roll_when']['pnl'] * 100,1)}",
                fg="blue",
            )
            return True

        return False

    def call_can_be_rolled(self, call):
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
                f"{call.contract.localSymbol} can be rolled because P&L of {round(pnl * 100, 1)}% is >= {round(self.config['roll_when']['pnl'] * 100,1)}",
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

    def manage(self, account_summary, portfolio_positions):
        click.secho("\nChecking positions...\n", fg="green")

        portfolio_positions = self.filter_positions(portfolio_positions)

        self.check_puts(portfolio_positions)
        self.check_calls(portfolio_positions)

        # Shut it down
        self.completion_future.set_result(True)

    def check_puts(self, portfolio_positions):
        # Check for puts which may be rolled to the next expiration or a better price
        puts = self.get_puts(portfolio_positions)

        # find puts eligible to be rolled
        rollable_puts = list(filter(lambda p: self.put_can_be_rolled(p), puts))

        click.secho(f"{len(rollable_puts)} puts will be rolled", fg="green")

        self.roll_puts(rollable_puts)

    def check_calls(self, portfolio_positions):
        # Check for calls which may be rolled to the next expiration or a better price
        calls = self.get_calls(portfolio_positions)

        # find calls eligible to be rolled
        rollable_calls = list(filter(lambda p: self.call_can_be_rolled(p), calls))

        click.secho(f"{len(rollable_calls)} calls will be rolled", fg="green")

        self.roll_calls(rollable_calls)

    def roll_puts(self, puts):
        return self.roll_positions(puts, "P")

    def roll_calls(self, calls):
        return self.roll_positions(calls, "C")

    def roll_positions(self, positions, right):
        for position in positions:
            symbol = position.contract.symbol
            sell_ticker = self.find_eligible_contracts(symbol, right)
            quantity = abs(position.position)

            position.contract.exchange = "SMART"
            [buy_ticker] = self.ib.reqTickers(position.contract)

            price = buy_ticker.modelGreeks.optPrice - sell_ticker.modelGreeks.optPrice

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
                price,
                algoStrategy="Adaptive",
                algoParams=[TagValue("adaptivePriority", "Patient")],
            )
            # Submit order
            trade = self.ib.placeOrder(combo, order)
            click.secho("Order submitted", fg="green")
            click.secho(f"{trade}", fg="green")

    def find_eligible_contracts(self, symbol, right):
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
            # The open interest value is never present when using historical
            # data, so just ignore it when the value is None
            if right.startswith("P"):
                return (
                    math.isnan(ticker.putOpenInterest) or ticker.putOpenInterest is None
                ) or ticker.putOpenInterest >= self.config["target"][
                    "minimum_open_interest"
                ]
            if right.startswith("C"):
                return (
                    math.isnan(ticker.callOpenInterest)
                    or ticker.callOpenInterest is None
                ) or ticker.callOpenInterest >= self.config["target"][
                    "minimum_open_interest"
                ]

        def delta_is_valid(ticker):
            return (
                ticker.modelGreeks
                and ticker.modelGreeks.delta
                and abs(ticker.modelGreeks.delta) <= self.config["target"]["delta"]
            )

        # Filter by delta and open interest
        tickers = [
            ticker
            for ticker in tickers
            if delta_is_valid(ticker) and open_interest_is_valid(ticker)
        ]
        tickers = sorted(
            sorted(tickers, key=lambda t: t.modelGreeks.delta),
            key=lambda t: option_dte(t.contract.lastTradeDateOrContractMonth),
        )

        if len(tickers) == 0:
            raise RuntimeError(f"No valid contracts found for {symbol}. Aborting.")

        return tickers[0]
