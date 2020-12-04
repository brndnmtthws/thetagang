import click
import sys
import json
import datetime
from pygments import highlight
from pygments.formatters import TerminalFormatter
from pygments.lexers import JsonLexer


CONTEXT_SETTINGS = dict(
    help_option_names=["-h", "--help"], auto_envvar_prefix="THETAGANG"
)


@click.command(context_settings=CONTEXT_SETTINGS)
@click.option("--userid", help="Login for your IBKR account", required=True)
@click.option("--password", help="Password for your IBKR account", required=True)
@click.option("--twsVersion", help="Version of IBKR TWS", required=True, default="981")
@click.option("--tradingMode")
@click.option("--twsPath")
@click.option("--twsSettingsPath")
@click.option("--ibcPath")
@click.option("--ibcIni")
@click.option("--javaPath")
def cli(**kwargs):
    """ThetaGang is an IBKR bot for collecting money.

    You may specify options using environment variables by prefixing them
    with `THETAGANG_`. For example, you can specify the login details using
    `THETAGANG_IBLOGIN`.
    """

    from .thetagang import start

    start(**kwargs)
