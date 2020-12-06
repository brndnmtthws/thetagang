import pprint
import click
from ib_insync.contract import Option
from .options import contract_date_to_datetime, option_dte

pp = pprint.PrettyPrinter(indent=4)


class PortfolioManager:
    def __init__(self, config, ib, completion_future):
        self.config = config
        self.ib = ib
        self.completion_future = completion_future

    def get_calls(self, portfolio):
        return self.get_options(portfolio, "C")

    def get_puts(self, portfolio):
        return self.get_options(portfolio, "P")

    def get_options(self, portfolio, right):
        return list(
            filter(
                lambda p: (
                    isinstance(p.contract, Option)
                    and p.contract.right.startswith(right)
                ),
                portfolio,
            )
        )

    def put_can_be_rolled(self, put):
        dte = option_dte(put.contract)
        print(dte)
        return False

    def manage(self, account_summary, portfolio):
        self.check_puts(portfolio)
        self.check_calls(portfolio)

        # Shut it down
        self.completion_future.set_result(True)

    def check_puts(self, portfolio):
        # Check for puts which may be rolled to the next expiration or a better price

        puts = self.get_puts(portfolio)

        # find puts eligible to be rolled
        rollable_puts = list(filter(lambda p: self.put_can_be_rolled(p), puts))

    def check_calls(self, portfolio):
        # Check for calls which may be rolled to the next expiration or a better price
        calls = self.get_calls(portfolio)
