import math
from typing import Any, Dict

from rich.console import Console
from schema import And, Optional, Or, Schema

import thetagang.config_defaults as config_defaults
from thetagang.dict_merge import dict_merge

error_console = Console(stderr=True, style="bold red")
console = Console()


def normalize_config(config: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    # Do any pre-processing necessary to the config here, such as handling
    # defaults, deprecated values, config changes, etc.

    if "twsVersion" in config["ibc"]:
        error_console.print(
            "WARNING: config param ibc.twsVersion is deprecated, please remove it from your config.",
        )

        # TWS version is pinned to latest stable, delete any existing config if it's present
        del config["ibc"]["twsVersion"]

    if "maximum_new_contracts" in config["target"]:
        error_console.print(
            "WARNING: config param target.maximum_new_contracts is deprecated, please remove it from your config.",
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

    if (
        "close_at_pnl" in config["roll_when"]
        and config["roll_when"]["close_at_pnl"]
        and config["roll_when"]["close_at_pnl"] <= config["roll_when"]["min_pnl"]
    ):
        raise RuntimeError(
            "ERROR: roll_when.close_at_pnl needs to be greater than roll_when.min_pnl."
        )

    return apply_default_values(config)


def apply_default_values(
    config: Dict[str, Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    return dict_merge(config_defaults.DEFAULT_CONFIG, config)


def validate_config(config: Dict[str, Dict[str, Any]]) -> None:
    if "minimum_cushion" in config["account"]:
        raise RuntimeError(
            "Config error: minimum_cushion is deprecated and replaced with margin_usage. See sample config for details."
        )

    algo_settings = {
        "strategy": And(str, len),
        "params": [And([str], lambda p: len(p) == 2)],  # type: ignore
    }

    schema = Schema(
        {
            "account": {
                "number": And(str, len),
                "cancel_orders": bool,
                "margin_usage": And(float, lambda n: 0 <= n),
                "market_data_type": And(int, lambda n: 1 <= n <= 4),
            },
            "orders": {
                Optional("exchange"): And(str, len),
                Optional("algo"): algo_settings,
                Optional("price_update_delay"): And([int], lambda p: len(p) == 2),  # type: ignore
                Optional("minimum_credit"): And(float, lambda n: 0 <= n),
            },
            "option_chains": {
                "expirations": And(int, lambda n: 1 <= n),
                "strikes": And(int, lambda n: 1 <= n),
            },
            Optional("write_when"): {
                Optional("calculate_net_contracts"): bool,
                Optional("calls"): {
                    Optional("green"): bool,
                    Optional("red"): bool,
                    Optional("cap_factor"): And(float, lambda n: 0 <= n <= 1),
                    Optional("cap_target_floor"): And(float, lambda n: 0 <= n <= 1),
                },
                Optional("puts"): {
                    Optional("green"): bool,
                    Optional("red"): bool,
                },
            },
            "roll_when": {
                "pnl": And(float, lambda n: 0 <= n <= 1),
                "dte": And(int, lambda n: 0 <= n),
                "min_pnl": float,
                Optional("close_at_pnl"): float,
                Optional("max_dte"): And(int, lambda n: 1 <= n),
                Optional("calls"): {
                    Optional("itm"): bool,
                    Optional("credit_only"): bool,
                    Optional("has_excess"): bool,
                    Optional("maintain_high_water_mark"): bool,
                },
                Optional("puts"): {
                    Optional("itm"): bool,
                    Optional("credit_only"): bool,
                    Optional("has_excess"): bool,
                },
            },
            "target": {
                "dte": And(int, lambda n: 0 <= n),
                "delta": And(float, lambda n: 0 <= n <= 1),
                Optional("max_dte"): And(int, lambda n: 1 <= n),
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
                    Optional("write_threshold"): And(float, lambda n: 0 <= n <= 1),
                    Optional("write_threshold_sigma"): And(float, lambda n: n > 0),
                    Optional("max_dte"): And(int, lambda n: 1 <= n),
                    Optional("calls"): {
                        Optional("delta"): And(float, lambda n: 0 <= n <= 1),
                        Optional("write_threshold"): And(float, lambda n: 0 <= n <= 1),
                        Optional("write_threshold_sigma"): And(float, lambda n: n > 0),
                        Optional("strike_limit"): And(float, lambda n: n > 0),
                        Optional("maintain_high_water_mark"): bool,
                        Optional("cap_factor"): And(float, lambda n: 0 <= n <= 1),
                        Optional("cap_target_floor"): And(float, lambda n: 0 <= n <= 1),
                    },
                    Optional("puts"): {
                        Optional("delta"): And(float, lambda n: 0 <= n <= 1),
                        Optional("write_threshold"): And(float, lambda n: 0 <= n <= 1),
                        Optional("write_threshold_sigma"): And(float, lambda n: n > 0),
                        Optional("strike_limit"): And(float, lambda n: n > 0),
                    },
                    Optional("adjust_price_after_delay"): bool,
                }
            },
            Optional("ib_insync"): {
                Optional("logfile"): And(str, len),
                Optional("api_response_wait_time"): int,
            },
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
            Optional("vix_call_hedge"): {
                "enabled": bool,
                Optional("delta"): And(float, lambda n: 0 <= n <= 1),
                Optional("target_dte"): And(int, lambda n: n > 0),
                Optional("close_hedges_when_vix_exceeds"): float,
                Optional("ignore_dte"): And(int, lambda n: n >= 0),
                Optional("max_dte"): And(int, lambda n: 1 <= n),
                Optional("allocation"): [
                    {
                        Optional("lower_bound"): float,
                        Optional("upper_bound"): float,
                        Optional("weight"): float,
                    },
                ],
            },
            Optional("cash_management"): {
                Optional("enabled"): bool,
                Optional("cash_fund"): And(str, len),
                Optional("primary_exchange"): And(str, len),
                Optional("target_cash_balance"): int,
                Optional("buy_threshold"): And(int, lambda n: n > 0),
                Optional("sell_threshold"): And(int, lambda n: n > 0),
                Optional("primary_exchange"): And(str, len),
                Optional("orders"): {
                    "exchange": And(str, len),
                    "algo": algo_settings,
                },
            },
            Optional("constants"): {
                Optional("daily_stddev_window"): And(str, len),
                Optional("write_threshold"): And(float, lambda n: 0 <= n <= 1),
                Optional("write_threshold_sigma"): And(float, lambda n: n > 0),
                Optional("calls"): {
                    Optional("write_threshold"): And(float, lambda n: 0 <= n <= 1),
                    Optional("write_threshold_sigma"): And(float, lambda n: n > 0),
                },
                Optional("puts"): {
                    Optional("write_threshold"): And(float, lambda n: 0 <= n <= 1),
                    Optional("write_threshold_sigma"): And(float, lambda n: n > 0),
                },
            },
        }
    )
    schema.validate(config)  # type: ignore

    assert len(config["symbols"]) > 0

    assert math.isclose(
        1, sum([s["weight"] for s in config["symbols"].values()]), rel_tol=1e-5
    )
