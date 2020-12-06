#!/usr/bin/env python

import copy
import asyncio
from ib_insync import IBC, IB, Watchdog, Index, util
import pprint
import click
from ib_insync.objects import Position
from .portfolio_manager import PortfolioManager

from ib_insync.contract import Stock
from .util import (
    account_summary_to_dict,
    justify,
    portfolio_positions_to_dict,
    position_pnl,
    to_camel_case,
)

pp = pprint.PrettyPrinter(indent=2)

util.patchAsyncio()


def start(**kwargs):
    import toml

    with open(kwargs.get("config"), "r") as f:
        config = toml.load(f)

    click.secho(f"Config:", fg="green")

    click.secho(f"\n  Account details:", fg="green")
    click.secho(
        f"    Number                   = {config['account']['number']}", fg="cyan"
    )
    click.secho(
        f"    Cancel existing orders   = {config['account']['cancel_orders']}",
        fg="cyan",
    )
    click.secho(
        f"    Minimum excess liquidity = {config['account']['minimum_excess_liquidity']} ({config['account']['minimum_excess_liquidity'] * 100}%)",
        fg="cyan",
    )
    click.secho(
        f"    Market data type         = {config['account']['market_data_type']}",
        fg="cyan",
    )

    click.secho(f"\n  Roll options when either condition is true:", fg="green")
    click.secho(
        f"    Days to expiry          <= {config['roll_when']['dte']}", fg="cyan"
    )
    click.secho(
        f"    P&L                     >= {config['roll_when']['pnl']} ({config['roll_when']['pnl'] * 100}%)",
        fg="cyan",
    )

    click.secho(f"\n  Write options with targets of:", fg="green")
    click.secho(f"    Days to expiry          >= {config['target']['dte']}", fg="cyan")
    click.secho(
        f"    Delta                   <= {config['target']['delta']}", fg="cyan"
    )
    click.secho(
        f"    Minimum open interest   >= {config['target']['minimum_open_interest']}",
        fg="cyan",
    )

    click.secho(f"\n  Symbols:", fg="green")
    for s in config["symbols"].keys():
        click.secho(
            f"    {s}, weight = {config['symbols'][s]['weight']} ({config['symbols'][s]['weight'] * 100}%)",
            fg="cyan",
        )
    assert (
        sum([config["symbols"][s]["weight"] for s in config["symbols"].keys()]) == 1.0
    )

    camelcase_kwargs = dict()
    # add in camel case copy of args
    for k in kwargs.keys():
        if k.startswith("ibkr_"):
            camelcase_kwargs[to_camel_case(k[5:])] = kwargs[k]

    ibc = IBC(**camelcase_kwargs)

    def onConnected():
        ib.reqMarketDataType(config["account"]["market_data_type"])

        if config["account"]["cancel_orders"]:
            # Cancel any existing orders
            open_orders = ib.openOrders()
            for order in open_orders:
                if order.isActive() and order.contract.symbol in config["symbols"]:
                    click.secho(f"Canceling order {order}", fg="red")
                    ib.cancelOrder(order)

        account_summary = ib.accountSummary(config["account"]["number"])
        click.secho(f"\nAccount summary:\n", fg="green")
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
        click.secho(
            f"  Cushion           = {account_summary['Cushion'].value} ({round(float(account_summary['Cushion'].value) * 100, 1)}%)",
            fg="cyan",
        )

        portfolio_positions = ib.portfolio()
        portfolio_positions = list(
            filter(
                lambda p: p.account == config["account"]["number"], portfolio_positions
            )
        )

        click.secho("\nPortfolio positions:", fg="green")
        portfolio_positions = portfolio_positions_to_dict(portfolio_positions)
        for symbol in portfolio_positions.keys():
            click.secho(f"\n  {symbol}:", fg="cyan")
            for p in portfolio_positions[symbol]:
                click.secho(f"    {p.contract}", fg="cyan")
                click.secho(f"      P&L {round(position_pnl(p) * 100, 1)}%", fg="cyan")

        portfolio_manager.manage(account_summary, portfolio_positions)

    ib = IB()
    ib.connectedEvent += onConnected

    completion_future = asyncio.Future()
    portfolio_manager = PortfolioManager(config, ib, completion_future)

    watchdog = Watchdog(ibc, ib, port=4002, probeContract=Stock("SPY", "SMART", "USD"))

    watchdog.start()
    ib.run(completion_future)
    watchdog.stop()
    ibc.terminate()
