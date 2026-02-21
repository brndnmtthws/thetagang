import logging

import click
import click_log

logger = logging.getLogger(__name__)
click_log.basic_config(logger)


CONTEXT_SETTINGS = dict(
    help_option_names=["-h", "--help"], auto_envvar_prefix="THETAGANG"
)


@click.command(context_settings=CONTEXT_SETTINGS)
@click_log.simple_verbosity_option(logger, default="WARNING")
@click.option(
    "-c",
    "--config",
    help="Path to toml config",
    required=True,
    default="thetagang.toml",
    type=click.Path(exists=True, readable=True),
)
@click.option(
    "--without-ibc",
    is_flag=True,
    help="Run without IBC. Enable this if you want to run the TWS "
    "gateway yourself, without having ThetaGang manage it for you.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Perform a dry run. This will display the the orders without sending any live trades.",
)
@click.option(
    "--migrate-config",
    is_flag=True,
    help="Migrate a v1 config file to v2 and exit without running trading logic.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Automatically approve config migration prompts.",
)
def cli(
    config: str,
    without_ibc: bool,
    dry_run: bool,
    migrate_config: bool,
    yes: bool,
) -> None:
    """ThetaGang is an IBKR bot for collecting money.

    You can configure this tool by supplying a toml configuration file.
    There's a sample config on GitHub, here:
    https://github.com/brndnmtthws/thetagang/blob/main/thetagang.toml
    """

    if logger.getEffectiveLevel() > logging.INFO:
        logging.getLogger("alembic").setLevel(logging.WARNING)
        logging.getLogger("alembic.runtime").setLevel(logging.WARNING)
        logging.getLogger("alembic.runtime.migration").setLevel(logging.WARNING)
        logging.getLogger("ib_async").setLevel(logging.WARNING)
        logging.getLogger("ib_async.client").setLevel(logging.WARNING)

    from thetagang.config_migration.startup_migration import (
        InvalidMigrationOptionError,
        MigrationDeclinedError,
        MigrationPreviewRedactionError,
        MigrationRequiredError,
        UnknownSchemaError,
    )

    from .thetagang import start

    try:
        start(
            config,
            without_ibc,
            dry_run,
            migrate_config=migrate_config,
            auto_approve_migration=yes,
        )
    except (
        InvalidMigrationOptionError,
        MigrationDeclinedError,
        MigrationPreviewRedactionError,
        MigrationRequiredError,
        UnknownSchemaError,
    ) as exc:
        raise click.ClickException(str(exc)) from exc
