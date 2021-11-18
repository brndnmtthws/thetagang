import math

import click
from schema import And, Optional, Or, Schema

import thetagang.config_defaults as config_defaults
from thetagang.dict_merge import dict_merge


def normalize_config(config):
    # Do any pre-processing necessary to the config here, such as handling
    # defaults, deprecated values, config changes, etc.

    if "twsVersion" in config["ibc"]:
        click.secho(
            "WARNING: config param ibc.twsVersion is deprecated, please remove it from your config.",
            fg="yellow",
            err=True,
        )

        # TWS version is pinned to latest stable, delete any existing config if it's present
        del config["ibc"]["twsVersion"]

    if "maximum_new_contracts" in config["target"]:
        click.secho(
            "WARNING: config param target.maximum_new_contracts is deprecated, please remove it from your config.",
            fg="yellow",
            err=True,
        )

        del config["target"]["maximum_new_contracts"]

    # xor: should have weight OR parts, but not both
    if any(["weight" in s for s in config["symbols"].values()]) == any(
        ["parts" in s for s in config["symbols"].values()]
    ):
        raise RuntimeError(
            "ERROR: all symbols should have either a weight or parts specified, but parts and weights cannot be mixed."
        )

    if "parts" in list(config["symbols"].values())[0]:
        # If using "parts" instead of "weight", convert parts into weights
        total_parts = float(sum([s["parts"] for s in config["symbols"].values()]))
        for k in config["symbols"].keys():
            config["symbols"][k]["weight"] = config["symbols"][k]["parts"] / total_parts
        for s in config["symbols"].values():
            del s["parts"]

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
                Optional("max_dte"): And(int, lambda n: 1 <= n),
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
                Optional("maximum_new_contracts"): And(int, lambda n: 1 <= n),
                Optional("maximum_new_contracts_percent"): And(
                    float, lambda n: 0 <= n <= 1
                ),
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
                    Or("weight", "parts", only_one=True): And(
                        Or(float, int),
                        lambda n: 0 <= n <= 1 if isinstance(n, float) else n > 0,
                    ),
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
                Optional("RaiseRequestErrors"): bool,
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
