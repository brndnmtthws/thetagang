import asyncio
from asyncio import Future
from pathlib import Path
from typing import Any, Awaitable, Optional, Protocol, cast

import tomlkit
from ib_async import IB, IBC, Contract, Watchdog, util
from rich.console import Console

from thetagang import log
from thetagang.config import Config, stage_enabled_map
from thetagang.config_migration.startup_migration import (
    run_startup_migration,
)
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


def _configure_ib_async_logging(logfile: Optional[str]) -> None:
    if not logfile:
        return

    path = Path(logfile).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        util.logToFile(str(path))
    except OSError as exc:
        log.warning(
            f"Unable to initialize ib_async logfile at {path}: {exc}. "
            "Continuing without file logging."
        )


def start(
    config_path: str,
    without_ibc: bool = False,
    dry_run: bool = False,
    *,
    migrate_config: bool = False,
    auto_approve_migration: bool = False,
) -> None:
    migration_flow = run_startup_migration(
        config_path,
        migrate_only=migrate_config,
        auto_approve=auto_approve_migration,
    )

    raw_config = migration_flow.config_text
    if migrate_config:
        if migration_flow.was_migrated:
            console.print(
                "Migration complete. Exiting because --migrate-config was set."
            )
        else:
            console.print(
                "Config already uses schema v2. Exiting because --migrate-config was set."
            )
        return

    config_doc = tomlkit.parse(raw_config).unwrap()
    config = Config(**config_doc)
    run_stage_flags = stage_enabled_map(config)

    config.display(config_path)

    data_store = None
    if config.runtime.database.enabled:
        db_url = config.runtime.database.resolve_url(config_path)
        sqlite_path = sqlite_db_path(db_url)
        if sqlite_path:
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        data_store = DataStore(db_url, config_path, dry_run, raw_config)

    _configure_ib_async_logging(config.runtime.ib_async.logfile)

    # Check if exchange is open before continuing
    if need_to_exit(config.runtime.exchange_hours):
        return

    async def onConnected() -> None:
        log.info(f"Connected to IB Gateway, serverVersion={ib.client.serverVersion()}")
        await portfolio_manager.manage()

    ib = IB()
    ib.connectedEvent += onConnected

    completion_future: Future[bool] = util.getLoop().create_future()
    portfolio_manager = PortfolioManager(
        config,
        ib,
        completion_future,
        dry_run,
        data_store=data_store,
        run_stage_flags=run_stage_flags,
    )

    probe_contract_config = config.runtime.watchdog.probeContract
    watchdog_config = config.runtime.watchdog
    probeContract = Contract(
        secType=probe_contract_config.secType,
        symbol=probe_contract_config.symbol,
        currency=probe_contract_config.currency,
        exchange=probe_contract_config.exchange,
    )

    if not without_ibc:
        # TWS version is pinned to current stable
        ibc_config = config.runtime.ibc
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
            account=config.runtime.account.number,
        )
        cast(_IBRunner, ib).run(completion_future)
        ib.disconnect()
