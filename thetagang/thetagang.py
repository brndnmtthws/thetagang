#!/usr/bin/env python

import asyncio
import copy
import pprint

import click
from ib_insync import IB, IBC, Index, Watchdog, util
from ib_insync.contract import Stock
from ib_insync.objects import Position

from .portfolio_manager import PortfolioManager
from .util import (
    account_summary_to_dict,
    justify,
    portfolio_positions_to_dict,
    position_pnl,
    to_camel_case,
)

pp = pprint.PrettyPrinter(indent=2)

util.patchAsyncio()


def start(config):
    import toml

    with open(config, "r") as f:
        config = toml.load(f)

    click.secho(f"Config:", fg="green")
    click.echo()

    click.secho(f"  Account details:", fg="green")
    click.secho(
        f"    Number                   = {config['account']['number']}", fg="cyan"
    )
    click.secho(
        f"    Cancel existing orders   = {config['account']['cancel_orders']}",
        fg="cyan",
    )
    click.secho(
        f"    Minimum cushion          = {config['account']['minimum_cushion']} ({config['account']['minimum_cushion'] * 100}%)",
        fg="cyan",
    )
    click.secho(
        f"    Market data type         = {config['account']['market_data_type']}",
        fg="cyan",
    )
    click.echo()

    click.secho(f"  Roll options when either condition is true:", fg="green")
    click.secho(
        f"    Days to expiry          <= {config['roll_when']['dte']}", fg="cyan"
    )
    click.secho(
        f"    P&L                     >= {config['roll_when']['pnl']} ({config['roll_when']['pnl'] * 100}%)",
        fg="cyan",
    )

    click.echo()
    click.secho(f"  Write options with targets of:", fg="green")
    click.secho(f"    Days to expiry          >= {config['target']['dte']}", fg="cyan")
    click.secho(
        f"    Delta                   <= {config['target']['delta']}", fg="cyan"
    )
    click.secho(
        f"    Minimum open interest   >= {config['target']['minimum_open_interest']}",
        fg="cyan",
    )

    click.echo()
    click.secho(f"  Symbols:", fg="green")
    for s in config["symbols"].keys():
        click.secho(
            f"    {s}, weight = {config['symbols'][s]['weight']} ({config['symbols'][s]['weight'] * 100}%)",
            fg="cyan",
        )
    assert (
        sum([config["symbols"][s]["weight"] for s in config["symbols"].keys()]) == 1.0
    )
    click.echo()

    ibc = IBC(**config["ibc"])

    def onConnected():
        ib.reqMarketDataType(config["account"]["market_data_type"])

        if config["account"]["cancel_orders"]:
            # Cancel any existing orders
            open_trades = ib.openTrades()
            for trade in open_trades:
                if trade.isActive() and trade.contract.symbol in config["symbols"]:
                    click.secho(f"Canceling order {trade.order}", fg="red")
                    ib.cancelOrder(trade.order)

        account_summary = ib.accountSummary(config["account"]["number"])
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

        portfolio_positions = ib.portfolio()
        # Filter out any positions we don't care about, i.e., we don't know the
        # symbol or it's not in the desired account.
        portfolio_positions = [
            item
            for item in portfolio_positions
            if item.account == config["account"]["number"]
            and item.contract.symbol in config["symbols"]
        ]

        click.echo()
        click.secho("Portfolio positions:", fg="green")
        click.echo()
        portfolio_positions = portfolio_positions_to_dict(portfolio_positions)
        for symbol in portfolio_positions.keys():
            click.secho(f"  {symbol}:", fg="cyan")
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
