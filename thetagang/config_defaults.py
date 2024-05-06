from typing import Any, Dict

DEFAULT_CONFIG: Dict[str, Dict[str, Any]] = {
    "ib_insync": {
        "api_response_wait_time": 60,
    },
    "orders": {
        "exchange": "SMART",
        "price_update_delay": [30, 60],
        "minimum_credit": 0.0,
        "algo": {
            "strategy": "Adaptive",
            "params": [["adaptivePriority", "Patient"]],
        },
    },
    "target": {
        "maximum_new_contracts_percent": 0.05,
        "delta": 0.3,
    },
    "write_when": {
        "calculate_net_contracts": False,
        "puts": {"red": True, "green": False},
        "calls": {
            "green": True,
            "red": False,
            "cap_factor": 1.0,
            "cap_target_floor": 0.0,
        },
    },
    "roll_when": {
        "min_pnl": 0.0,
        "close_at_pnl": 1.0,
        "calls": {
            "itm": True,
            "credit_only": False,
            "has_excess": True,
            "maintain_high_water_mark": False,
        },
        "puts": {"itm": False, "credit_only": False, "has_excess": True},
    },
    "vix_call_hedge": {
        "enabled": False,
        "delta": 0.3,
        "target_dte": 30,
        "ignore_dte": 0,
        "allocation": [
            {"upper_bound": 15.0, "weight": 0.0},
            {"lower_bound": 15.0, "upper_bound": 30.0, "weight": 0.01},
            {"lower_bound": 30.0, "upper_bound": 50.0, "weight": 0.005},
            {"lower_bound": 50.0, "weight": 0.0},
        ],
    },
    "cash_management": {
        "enabled": False,
        "cash_fund": "SGOV",
        "target_cash_balance": 0,
        "buy_threshold": 10000,
        "sell_threshold": 10000,
        "orders": {"exchange": "SMART", "algo": {"strategy": "Vwap", "params": []}},
    },
    "constants": {"daily_stddev_window": "30 D"},
}
