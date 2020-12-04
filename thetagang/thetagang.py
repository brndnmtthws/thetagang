#!/usr/bin/env python

import copy
import asyncio
from ib_insync import IBC, IB, Watchdog, Index, util
import pprint
from .util import to_camel_case

pp = pprint.PrettyPrinter(indent=4)

util.patchAsyncio()


def start(**kwargs):
    camelcase_kwargs = dict()
    # add in camel case copy of args
    for k in kwargs.keys():
        if k.startswith("ibkr_"):
            camelcase_kwargs[to_camel_case(k[5:])] = kwargs[k]

    ibc = IBC(**camelcase_kwargs)

    def onConnected():
        pp.pprint(ib.accountValues())

        spx = Index("SPX", "CBOE")
        contracts = ib.qualifyContracts(spx)
        pp.pprint(contracts)

    ib = IB()
    ib.connectedEvent += onConnected

    watchdog = Watchdog(ibc, ib, port=4002)

    watchdog.start()
    ib.run()
