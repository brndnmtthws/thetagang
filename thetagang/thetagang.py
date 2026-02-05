import asyncio
from asyncio import Future
from typing import Any, Awaitable, Protocol, cast

import toml
from ib_async import IB, IBC, Contract, Watchdog, util
from rich.console import Console

from thetagang import log
from thetagang.config import Config, normalize_config
from thetagang.db import DataStore, sqlite_db_path
from thetagang.exchange_hours import need_to_exit
from thetagang.portfolio_manager import PortfolioManager


class _IBRunner(Protocol):
    def run(self, awaitable: Awaitable[Any]) -> Any: ...


try:
    asyncio.get_running_loop()
except RuntimeError:
    pass
else:
    util.patchAsyncio()

console = Console()


def start(config_path: str, without_ibc: bool = False, dry_run: bool = False) -> None:
    with open(config_path, "r", encoding="utf8") as file:
        raw_config = file.read()
        config = toml.loads(raw_config)

    config = Config(**normalize_config(config))  # type: ignore

    config.display(config_path)

    data_store = None
    if config.database.enabled:
        db_url = config.database.resolve_url(config_path)
        sqlite_path = sqlite_db_path(db_url)
        if sqlite_path:
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        data_store = DataStore(db_url, config_path, dry_run, raw_config)

    if config.ib_async.logfile:
        util.logToFile(config.ib_async.logfile)

    # Check if exchange is open before continuing
    if need_to_exit(config.exchange_hours):
        return

    async def onConnected() -> None:
        log.info(f"Connected to IB Gateway, serverVersion={ib.client.serverVersion()}")
        await portfolio_manager.manage()

    ib = IB()
    ib.connectedEvent += onConnected

    completion_future: Future[bool] = util.getLoop().create_future()
    portfolio_manager = PortfolioManager(
        config, ib, completion_future, dry_run, data_store=data_store
    )

    probe_contract_config = config.watchdog.probeContract
    watchdog_config = config.watchdog
    probeContract = Contract(
        secType=probe_contract_config.secType,
        symbol=probe_contract_config.symbol,
        currency=probe_contract_config.currency,
        exchange=probe_contract_config.exchange,
    )

    if not without_ibc:
        # TWS version is pinned to current stable
        ibc_config = config.ibc
        ibc = IBC(1037, **ibc_config.to_dict())
        log.info(f"Starting TWS with twsVersion={ibc.twsVersion}")

        ib.RaiseRequestErrors = ibc_config.RaiseRequestErrors

        watchdog = Watchdog(
            ibc, ib, probeContract=probeContract, **watchdog_config.to_dict()
        )

        async def run_with_watchdog() -> None:
            watchdog.start()
            try:
                await completion_future
            finally:
                watchdog.stop()
                await ibc.terminateAsync()

        cast(_IBRunner, ib).run(run_with_watchdog())
    else:
        ib.connect(
            watchdog_config.host,
            watchdog_config.port,
            clientId=watchdog_config.clientId,
            timeout=watchdog_config.probeTimeout,
            account=config.account.number,
        )
        cast(_IBRunner, ib).run(completion_future)
        ib.disconnect()
