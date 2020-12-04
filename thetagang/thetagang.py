#!/usr/bin/env python

import copy
import logging
import asyncio
from thetagang.portfolio import Portfolio
from ib_insync import IBC, IB, Watchdog, Index, util
import pprint

from ib_insync.contract import Stock
from .util import to_camel_case

pp = pprint.PrettyPrinter(indent=4)

util.patchAsyncio()

logging.basicConfig(level=logging.INFO)


def start(**kwargs):
    import toml

    with open(kwargs.get("config"), "r") as f:
        config = toml.load(f)
    logging.info(f"Loaded config:")
    logging.info(pp.pformat(config))

    camelcase_kwargs = dict()
    # add in camel case copy of args
    for k in kwargs.keys():
        if k.startswith("ibkr_"):
            camelcase_kwargs[to_camel_case(k[5:])] = kwargs[k]

    ibc = IBC(**camelcase_kwargs)

    def onConnected():
        account_summary = ib.accountSummary(config["account"]["number"])
        logging.info(f"Account summary:")
        logging.info(pp.pformat(account_summary))

        portfolio = ib.portfolio()
        portfolio = list(
            filter(lambda p: p.account == config["account"]["number"], portfolio)
        )
        logging.info("Portfolio:")
        logging.info(pp.pformat(portfolio))

        from .portfolio import Portfolio

        Portfolio(ib).manage(account_summary, portfolio)

    ib = IB()
    ib.connectedEvent += onConnected

    watchdog = Watchdog(ibc, ib, port=4002, probeContract=Stock("SPY", "SMART", "USD"))

    watchdog.start()
    ib.run()
