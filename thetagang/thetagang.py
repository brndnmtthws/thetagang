#!/usr/bin/env python

import asyncio

import click
from ib_insync import IB, IBC, Watchdog, util
from ib_insync.contract import Contract

from thetagang.config import normalize_config, validate_config
from thetagang.util import get_strike_limit, get_target_delta

from .portfolio_manager import PortfolioManager

util.patchAsyncio()


def start(config):
    import toml

    import thetagang.config_defaults as config_defaults  # NOQA

    with open(config, "r") as f:
        config = toml.load(f)

    config = normalize_config(config)

    validate_config(config)

    click.secho("Config:", fg="green")
    click.echo()

    click.secho("  Account details:", fg="green")
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

    click.secho("  Order settings:", fg="green")
    click.secho(
        f"    Exchange                 = {config['orders']['exchange']}",
        fg="cyan",
    )
    click.secho(
        f"    Strategy                 = {config['orders']['algo']['strategy']}",
        fg="cyan",
    )
    click.secho(
        f"    Params                   = {config['orders']['algo']['params']}",
        fg="cyan",
    )
    click.echo()

    if config["roll_when"]["close_at_pnl"] < 1.0:
        click.secho(
            f"  Close options when P&L >= {config['roll_when']['close_at_pnl'] * 100}%",
            fg="green",
        )
    click.secho("  Roll options when either condition is true:", fg="green")
    click.secho(
        f"    Days to expiry          <= {config['roll_when']['dte']} and P&L >= {config['roll_when']['min_pnl']} ({config['roll_when']['min_pnl'] * 100}%)",
        fg="cyan",
    )
    if "max_dte" in config["roll_when"]:
        click.secho(
            f"    P&L                     >= {config['roll_when']['pnl']} ({config['roll_when']['pnl'] * 100}%) and DTE < {config['roll_when']['max_dte']}",
            fg="cyan",
        )
    else:
        click.secho(
            f"    P&L                     >= {config['roll_when']['pnl']} ({config['roll_when']['pnl'] * 100}%)",
            fg="cyan",
        )

    click.echo()
    click.secho("  For underlying, only write new contracts when:", fg="green")
    click.secho(
        f"    Puts, red                = {config['write_when']['puts']['red']}",
        fg="cyan",
    )
    click.secho(
        f"    Calls, green             = {config['write_when']['calls']['green']}",
        fg="cyan",
    )

    click.echo()
    click.secho("  When contracts are ITM:", fg="green")
    click.secho(
        f"    Roll puts                = {config['roll_when']['puts']['itm']}",
        fg="cyan",
    )
    click.secho(
        f"    Roll calls               = {config['roll_when']['calls']['itm']}",
        fg="cyan",
    )

    click.echo()
    click.secho("  Write options with targets of:", fg="green")
    click.secho(f"    Days to expiry          >= {config['target']['dte']}", fg="cyan")
    click.secho(
        f"    Default delta           <= {config['target']['delta']}", fg="cyan"
    )
    if "puts" in config["target"]:
        click.secho(
            f"    Delta for puts          <= {config['target']['puts']['delta']}",
            fg="cyan",
        )
    if "calls" in config["target"]:
        click.secho(
            f"    Delta for calls         <= {config['target']['calls']['delta']}",
            fg="cyan",
        )
    click.secho(
        f"    Maximum new contracts    = {config['target']['maximum_new_contracts_percent'] * 100}% of buying power",
        fg="cyan",
    )
    click.secho(
        f"    Minimum open interest    = {config['target']['minimum_open_interest']}",
        fg="cyan",
    )

    click.echo()
    click.secho("  Symbols:", fg="green")
    for s in config["symbols"].keys():
        c = config["symbols"][s]
        c_delta = f"{get_target_delta(config, s, 'C'):.2f}".rjust(4)
        p_delta = f"{get_target_delta(config, s, 'P'):.2f}".rjust(4)
        weight_p = f"{(c['weight'] * 100):.2f}".rjust(4)
        strike_limits = ""
        c_limit = get_strike_limit(config, s, "C")
        p_limit = get_strike_limit(config, s, "P")
        if c_limit:
            strike_limits += f", call strike >= ${c_limit:.2f}"
        if p_limit:
            strike_limits += f", put strike <= ${p_limit:.2f}"
        click.secho(
            f"    {s.rjust(5)} weight = {weight_p}%, delta = {p_delta}p, {c_delta}c{strike_limits}",
            fg="cyan",
        )
    assert (
        round(
            sum([config["symbols"][s]["weight"] for s in config["symbols"].keys()]), 5
        )
        == 1.00000
    )
    click.echo()

    if config.get("ib_insync", {}).get("logfile"):
        util.logToFile(config["ib_insync"]["logfile"])

    # TWS version is pinned to current stable
    ibc_config = config.get("ibc", {})
    # Remove any config params that aren't valid keywords for IBC
    ibc_keywords = {
        k: ibc_config[k] for k in ibc_config if k not in ["RaiseRequestErrors"]
    }
    ibc = IBC(1012, **ibc_keywords)

    def onConnected():
        portfolio_manager.manage()

    ib = IB()
    ib.RaiseRequestErrors = ibc_config.get("RaiseRequestErrors", False)
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
