DEFAULT_CONFIG = {
    "orders": {
        "exchange": "SMART",
        "algo": {
            "strategy": "Adaptive",
            "params": [["adaptivePriority", "Patient"]],
        },
    },
    "target": {
        "maximum_new_contracts_percent": 0.05,
        "delta": 0.3,
    },
    "write_when": {"puts": {"red": False}, "calls": {"green": False}},
    "roll_when": {
        "min_pnl": 0.0,
        "close_at_pnl": 1.0,
        "calls": {"itm": True},
        "puts": {"itm": False},
    },
}
