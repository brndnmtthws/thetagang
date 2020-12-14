import math

from schema import And, Optional, Schema, Use


def validate_config(config):
    schema = Schema(
        {
            "account": {
                "number": And(str, len),
                "cancel_orders": bool,
                "minimum_cushion": And(float, lambda n: 0 <= n <= 1),
                "market_data_type": And(int, lambda n: 1 <= n <= 4),
            },
            "option_chains": {
                "expirations": And(int, lambda n: 1 <= n),
                "strikes": And(int, lambda n: 1 <= n),
            },
            "roll_when": {
                "pnl": And(float, lambda n: 0 <= n <= 1),
                "dte": And(int, lambda n: 0 <= n),
            },
            "target": {
                "dte": And(int, lambda n: 0 <= n),
                "delta": And(float, lambda n: 0 <= n <= 1),
                "minimum_open_interest": And(int, lambda n: 0 <= n),
            },
            "symbols": {object: {"weight": And(float, lambda n: 0 <= n <= 1)}},
            Optional("ib_insync"): {Optional("logfile"): And(str, len)},
            "ibc": {
                Optional("password"): And(str, len),
                Optional("userid"): And(str, len),
                Optional("gateway"): bool,
                Optional("ibcPath"): And(str, len),
                Optional("tradingMode"): And(
                    str, len, lambda s: s in ("live", "paper")
                ),
                Optional("twsVersion"): int,
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
