#!/usr/bin/env python

import copy
import asyncio
from ib_insync import IBC, IB, Watchdog, Index, util
import pprint
import click
from ib_insync.objects import Position
from .portfolio_manager import PortfolioManager

from ib_insync.contract import Stock
from .util import to_camel_case

pp = pprint.PrettyPrinter(indent=2)

util.patchAsyncio()


def start(**kwargs):
    import toml

    with open(kwargs.get("config"), "r") as f:
        config = toml.load(f)

    click.secho(f"Loaded config:", fg="green")
    click.secho(pp.pformat(config), fg="cyan")

    camelcase_kwargs = dict()
    # add in camel case copy of args
    for k in kwargs.keys():
        if k.startswith("ibkr_"):
            camelcase_kwargs[to_camel_case(k[5:])] = kwargs[k]

    ibc = IBC(**camelcase_kwargs)

    def onConnected():
        account_summary = ib.accountSummary(config["account"]["number"])
        click.secho(f"Account summary:", fg="green")
        for a in account_summary:
            click.secho(pp.pformat(a), fg="cyan")

        portfolio = ib.portfolio()
        portfolio = list(
            filter(lambda p: p.account == config["account"]["number"], portfolio)
        )
        click.secho("Portfolio positions:", fg="green")
        for p in portfolio:
            click.secho(pp.pformat(p), fg="cyan")

        portfolio_manager.manage(account_summary, portfolio)

    ib = IB()
    ib.connectedEvent += onConnected

    completion_future = asyncio.Future()
    portfolio_manager = PortfolioManager(config, ib, completion_future)

    watchdog = Watchdog(ibc, ib, port=4002, probeContract=Stock("SPY", "SMART", "USD"))

    watchdog.start()
    ib.run(completion_future)
    watchdog.stop()
    ibc.terminate()
