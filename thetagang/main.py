import click
import sys
import json
import datetime
from pygments import highlight
from pygments.formatters import TerminalFormatter
from pygments.lexers import JsonLexer


CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


@click.command(context_settings=CONTEXT_SETTINGS)
@click.option("--login", help="Login", required=True)
@click.option("--password", help="Password", required=True)
def cli(
    login,
    password,
):
    """ThetaGang is an IBKR bot for collecting money"""

    click.echo(f"{login} {password}")

    return
