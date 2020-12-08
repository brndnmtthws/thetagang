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
    "-c",
    "--config",
    help="Path to toml config",
    required=True,
    default="thetagang.toml",
    type=click.Path(exists=True, readable=True),
)
def cli(config):
    """ThetaGang is an IBKR bot for collecting money.

    You can configure this tool by supplying a toml configuration file.
    There's a sample config on GitHub, here:
    https://github.com/brndnmtthws/thetagang/blob/main/thetagang.toml
    """

    from .thetagang import start

    start(config)
