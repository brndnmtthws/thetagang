import logging

import click
import click_log

logger = logging.getLogger(__name__)
click_log.basic_config(logger)  # type: ignore


CONTEXT_SETTINGS = dict(
    help_option_names=["-h", "--help"], auto_envvar_prefix="THETAGANG"
)


@click.command(context_settings=CONTEXT_SETTINGS)
@click_log.simple_verbosity_option(logger)  # type: ignore
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
    help=(
        "Skip legacy IBC management messaging. Ensure the IB Gateway "
        "is already running before invoking the CLI."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Retained for backwards compatibility. The CLI never submits trades "
        "so this flag only adjusts log messaging."
    ),
)
def cli(config: str, without_ibc: bool, dry_run: bool) -> None:
    """Display the IBKR account summary and any open positions."""

    from .thetagang import start

    start(config, without_ibc, dry_run)
