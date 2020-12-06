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
@click.option("--ibkr_userid", help="Login for your IBKR account", required=True)
@click.option("--ibkr_password", help="Password for your IBKR account", required=True)
@click.option(
    "--ibkr_tws_version", help="Version of IBKR TWS", required=True, default="981"
)
@click.option("--ibkr_trading_mode")
@click.option("--ibkr_tws_path")
@click.option("--ibkr_gateway/--no_ibkr_gateway", default=True)
@click.option("--ibkr_tws_settings_path")
@click.option("--ibkr_ibc_path")
@click.option("--ibkr_ibc_ini")
@click.option("--ibkr_java_path")
def cli(**kwargs):
    """ThetaGang is an IBKR bot for collecting money.

    You may specify options using environment variables by prefixing them
    with `THETAGANG_`. For example, you can specify the login details using
    `THETAGANG_IBKR_USERID`.

    Options prefixed with "ibkr_" are passed directly to the underlying IBC
    instance (see https://ib-insync.readthedocs.io/api.html#ibc for details).
    """

    from .thetagang import start

    start(**kwargs)
