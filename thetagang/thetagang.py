from asyncio import Future

import toml
from ib_async import IB, IBC, Contract, Watchdog, util
from rich.console import Console

from thetagang import log
from thetagang.config import Config, normalize_config
from thetagang.exchange_hours import need_to_exit
from thetagang.portfolio_manager import PortfolioManager

util.patchAsyncio()

console = Console()


def start(config_path: str, without_ibc: bool = False, dry_run: bool = False) -> None:
    with open(config_path, "r", encoding="utf8") as file:
        config = toml.load(file)

    config = Config(**normalize_config(config))  # type: ignore

    config.display(config_path)

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
    portfolio_manager = PortfolioManager(config, ib, completion_future, dry_run)

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
        watchdog.start()

        ib.run(completion_future)  # type: ignore
        watchdog.stop()
        ibc.terminate()
    else:
        ib.connect(
            watchdog_config.host,
            watchdog_config.port,
            clientId=watchdog_config.clientId,
            timeout=watchdog_config.probeTimeout,
            account=config.account.number,
        )
        ib.run(completion_future)  # type: ignore
        ib.disconnect()
