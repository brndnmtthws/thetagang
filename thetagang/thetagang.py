from __future__ import annotations

import toml
from ib_async import IB, util

from thetagang import log
from thetagang.config import Config
from thetagang.portfolio_manager import PortfolioManager

util.patchAsyncio()


def start(config_path: str, without_ibc: bool = False, dry_run: bool = False) -> None:
    with open(config_path, "r", encoding="utf8") as file:
        raw_config = toml.load(file)

    config = Config.from_dict(raw_config)
    config.display(config_path)

    if dry_run:
        log.notice("Dry-run flag detected: no trades are ever submitted by this CLI.")

    if not without_ibc:
        log.notice(
            "Automatic IB Gateway management is no longer available. "
            "Ensure the gateway is running and pass --without-ibc to suppress this message."
        )

    ib = IB()

    async def main() -> None:
        viewer = PortfolioManager(config, ib, log.console)
        await viewer.run()
        ib.disconnect()

    ib.connect(
        config.connection.host,
        config.connection.port,
        clientId=config.connection.client_id,
        account=config.account.number,
    )
    ib.reqMarketDataType(config.account.market_data_type)

    try:
        ib.run(main())  # type: ignore[arg-type]
    finally:
        if ib.isConnected():
            ib.disconnect()
