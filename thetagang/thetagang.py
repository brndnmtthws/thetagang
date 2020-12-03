#!/usr/bin/env python

import asyncio
from ib_insync import IBC, IB, Watchdog, Index, util

util.patchAsyncio()

ibc = IBC(
    981,
    gateway=True,
    tradingMode="paper",
    ibcPath="/Users/brenden/ibc",
    javaPath="/Library/Java/JavaVirtualMachines/jdk1.8.0_162.jdk/Contents/Home/bin",
)


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
