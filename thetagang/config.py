import math

import click
from schema import And, Optional, Schema, Use

import thetagang.config_defaults as config_defaults
from thetagang.dict_merge import dict_merge


def normalize_config(config):
    # Do any pre-processing necessary to the config here, such as handling
    # defaults, deprecated values, config changes, etc.

    if "twsVersion" in config["ibc"]:
        click.secho(
            "WARNING: IBC config param 'twsVersion' is deprecated, please remove it from your config.",
            fg="yellow",
            err=True,
        )

        # TWS version is pinned to latest stable, delete any existing config if it's present
        del config["ibc"]["twsVersion"]

    return apply_default_values(config)


def apply_default_values(config):
    return dict_merge(config_defaults.DEFAULT_CONFIG, config)


def validate_config(config):
    if "minimum_cushion" in config["account"]:
        raise "Config error: minimum_cushion is deprecated and replaced with margin_usage. See sample config for details."

    schema = Schema(
        {
            "account": {
                "number": And(str, len),
                "cancel_orders": bool,
                "margin_usage": And(float, lambda n: 0 <= n),
                "market_data_type": And(int, lambda n: 1 <= n <= 4),
            },
            "option_chains": {
                "expirations": And(int, lambda n: 1 <= n),
                "strikes": And(int, lambda n: 1 <= n),
            },
            "roll_when": {
                "pnl": And(float, lambda n: 0 <= n <= 1),
                "dte": And(int, lambda n: 0 <= n),
                "min_pnl": float,
                Optional("calls"): {
                    "itm": bool,
                },
                Optional("puts"): {
                    "itm": bool,
                },
            },
            "target": {
                "dte": And(int, lambda n: 0 <= n),
                "delta": And(float, lambda n: 0 <= n <= 1),
                "maximum_new_contracts": And(int, lambda n: 1 <= n),
                "minimum_open_interest": And(int, lambda n: 0 <= n),
                Optional("calls"): {
                    Optional("delta"): And(float, lambda n: 0 <= n <= 1),
                },
                Optional("puts"): {
                    Optional("delta"): And(float, lambda n: 0 <= n <= 1),
                },
            },
            "symbols": {
                object: {
                    "weight": And(float, lambda n: 0 <= n <= 1),
                    Optional("primary_exchange"): And(str, len),
                    Optional("delta"): And(float, lambda n: 0 <= n <= 1),
                    Optional("calls"): {
                        Optional("delta"): And(float, lambda n: 0 <= n <= 1),
                        Optional("strike_limit"): And(float, lambda n: n > 0),
                    },
                    Optional("puts"): {
                        Optional("delta"): And(float, lambda n: 0 <= n <= 1),
                        Optional("strike_limit"): And(float, lambda n: n > 0),
                    },
                }
            },
            Optional("ib_insync"): {Optional("logfile"): And(str, len)},
            "ibc": {
                Optional("password"): And(str, len),
                Optional("userid"): And(str, len),
                Optional("gateway"): bool,
                Optional("ibcPath"): And(str, len),
                Optional("tradingMode"): And(
                    str, len, lambda s: s in ("live", "paper")
                ),
                Optional("ibcIni"): And(str, len),
                Optional("twsPath"): And(str, len),
                Optional("twsSettingsPath"): And(str, len),
                Optional("javaPath"): And(str, len),
                Optional("fixuserid"): And(str, len),
                Optional("fixpassword"): And(str, len),
            },
            "watchdog": {
                Optional("appStartupTime"): int,
                Optional("appTimeout"): int,
                Optional("clientId"): int,
                Optional("connectTimeout"): int,
                Optional("host"): And(str, len),
                Optional("port"): int,
                Optional("probeTimeout"): int,
                Optional("readonly"): bool,
                Optional("retryDelay"): int,
                Optional("probeContract"): {
                    Optional("currency"): And(str, len),
                    Optional("exchange"): And(str, len),
                    Optional("secType"): And(str, len),
                    Optional("symbol"): And(str, len),
                },
            },
        }
    )
    schema.validate(config)

    assert len(config["symbols"]) > 0
    assert math.isclose(
        1, sum([s["weight"] for s in config["symbols"].values()]), rel_tol=1e-5
    )
