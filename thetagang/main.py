import logging

import click
import click_log

logger = logging.getLogger(__name__)
click_log.basic_config(logger)


CONTEXT_SETTINGS = dict(
    help_option_names=["-h", "--help"], auto_envvar_prefix="THETAGANG"
)


@click.command(context_settings=CONTEXT_SETTINGS)
@click_log.simple_verbosity_option(logger)
@click.option(
    "--config", help="Path to toml config", required=True, default="thetagang.toml"
)
def cli(config):
    """ThetaGang is an IBKR bot for collecting money."""

    from .thetagang import start

    start(config)
