#!/usr/bin/env python

import asyncio

import click
from ib_insync import IB, IBC, Index, Watchdog, util
from ib_insync.contract import Contract, Stock
from ib_insync.objects import Position

from thetagang.config import normalize_config, validate_config

from .portfolio_manager import PortfolioManager
from .util import (
    account_summary_to_dict,
    justify,
    portfolio_positions_to_dict,
    position_pnl,
    to_camel_case,
)

util.patchAsyncio()


def start(config):
    import toml

    with open(config, "r") as f:
        config = toml.load(f)

    config = normalize_config(config)

    validate_config(config)

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
        f"    Margin usage             = {config['account']['margin_usage']} ({config['account']['margin_usage'] * 100}%)",
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

    if config.get("ib_insync", {}).get("logfile"):
        util.logToFile(config["ib_insync"]["logfile"])

    ibc = IBC(978, **config["ibc"])

    def onConnected():
        portfolio_manager.manage()

    ib = IB()
    ib.connectedEvent += onConnected

    completion_future = asyncio.Future()
    portfolio_manager = PortfolioManager(config, ib, completion_future)

    probeContractConfig = config["watchdog"]["probeContract"]
    watchdogConfig = config.get("watchdog")
    del watchdogConfig["probeContract"]
    probeContract = Contract(
        secType=probeContractConfig["secType"],
        symbol=probeContractConfig["symbol"],
        currency=probeContractConfig["currency"],
        exchange=probeContractConfig["exchange"],
    )

    watchdog = Watchdog(ibc, ib, probeContract=probeContract, **watchdogConfig)

    watchdog.start()
    ib.run(completion_future)
    watchdog.stop()
    ibc.terminate()
