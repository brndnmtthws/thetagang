#!/usr/bin/env python

import asyncio
from ib_insync import IBC, IB, Watchdog, Index, util

util.patchAsyncio()


def start(**kwargs):
    ibc = IBC(kwargs.get("tws_version"), kwargs)

    def onConnected():
        print(ib.accountValues())

        spx = Index("SPX", "CBOE")
        contracts = ib.qualifyContracts(spx)
        print(contracts)

    ib = IB()
    ib.connectedEvent += onConnected

    watchdog = Watchdog(ibc, ib, port=4002)

    watchdog.start()
    ib.run()
