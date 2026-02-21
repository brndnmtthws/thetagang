import pytest

from thetagang.config import (
    Config,
    RebalanceMode,
    stage_enabled_map,
    stage_enabled_map_from_run,
)


def _base_config(run):
    return {
        "meta": {"schema_version": 2},
        "run": run,
        "runtime": {
            "account": {"number": "DUX", "margin_usage": 0.5},
            "option_chains": {"expirations": 4, "strikes": 10},
        },
        "portfolio": {"symbols": {"AAA": {"weight": 1.0}}},
        "strategies": {
            "wheel": {
                "defaults": {
                    "target": {"dte": 30, "minimum_open_interest": 5},
                    "roll_when": {"dte": 7},
                }
            }
        },
    }


def test_stage_enabled_map_reflects_compiled_strategy_flags() -> None:
    config = Config(**_base_config({"strategies": ["wheel", "cash_management"]}))
    flags = stage_enabled_map(config)
    assert flags["options_write_puts"] is True
    assert flags["equity_regime_rebalance"] is False
    assert flags["post_cash_management"] is True
    assert flags["post_vix_call_hedge"] is False


def test_run_config_rejects_unknown_strategy_id() -> None:
    with pytest.raises(ValueError, match="unknown strategy id"):
        Config(**_base_config({"strategies": ["wheel", "not-a-real-strategy"]}))


def test_run_config_rejects_duplicate_strategy_ids() -> None:
    with pytest.raises(ValueError, match="must not contain duplicates"):
        Config(**_base_config({"strategies": ["wheel", "wheel"]}))


def test_run_config_rejects_wheel_and_regime_together() -> None:
    with pytest.raises(
        ValueError, match="cannot enable wheel and regime_rebalance together"
    ):
        Config(**_base_config({"strategies": ["wheel", "regime_rebalance"]}))


def test_run_config_rejects_missing_run_plan() -> None:
    with pytest.raises(
        ValueError, match="must define at least one of run.strategies or run.stages"
    ):
        Config(**_base_config({}))


def test_run_config_rejects_both_strategies_and_stages() -> None:
    with pytest.raises(ValueError, match="must define exactly one"):
        Config(
            **_base_config(
                {
                    "strategies": ["wheel"],
                    "stages": [
                        {
                            "id": "options_write_puts",
                            "kind": "options.write_puts",
                            "enabled": True,
                        }
                    ],
                }
            )
        )


def test_explicit_run_stages_still_supported_for_advanced_mode() -> None:
    config = Config(
        **_base_config(
            {
                "stages": [
                    {
                        "id": "equity_regime_rebalance",
                        "kind": "equity.regime_rebalance",
                        "enabled": True,
                    },
                    {
                        "id": "post_cash_management",
                        "kind": "post.cash_management",
                        "enabled": True,
                        "depends_on": ["equity_regime_rebalance"],
                    },
                ]
            }
        )
    )
    flags = stage_enabled_map_from_run(config.run)
    assert flags["equity_regime_rebalance"] is True
    assert flags["post_cash_management"] is True


def test_explicit_run_config_rejects_enabled_stage_with_disabled_dependency() -> None:
    with pytest.raises(ValueError, match="depends on a disabled stage"):
        Config(
            **_base_config(
                {
                    "stages": [
                        {
                            "id": "options_write_puts",
                            "kind": "options.write_puts",
                            "enabled": False,
                        },
                        {
                            "id": "options_write_calls",
                            "kind": "options.write_calls",
                            "enabled": True,
                            "depends_on": ["options_write_puts"],
                        },
                    ]
                }
            )
        )


def test_explicit_run_config_rejects_unknown_stage_id() -> None:
    with pytest.raises(ValueError, match="unknown stage id"):
        Config(
            **_base_config(
                {
                    "stages": [
                        {"id": "oops", "kind": "options.write_puts", "enabled": False}
                    ]
                }
            )
        )


def test_explicit_run_config_rejects_mismatched_stage_kind() -> None:
    with pytest.raises(ValueError, match="kind must be"):
        Config(
            **_base_config(
                {
                    "stages": [
                        {
                            "id": "options_write_puts",
                            "kind": "options.write_calls",
                            "enabled": False,
                        }
                    ]
                }
            )
        )


def test_v2_to_legacy_does_not_materialize_absent_strategy_sections() -> None:
    config = Config(**_base_config({"strategies": ["wheel"]}))
    assert config.strategies.regime_rebalance.enabled is False
    assert config.strategies.vix_call_hedge.enabled is False
    assert config.strategies.cash_management.enabled is False


def test_v2_rejects_transitional_symbols_and_overrides() -> None:
    with pytest.raises(ValueError):
        Config.model_validate(
            {
                "meta": {"schema_version": 2},
                "run": {
                    "stages": [
                        {"id": "options_write_puts", "kind": "options.write_puts"}
                    ]
                },
                "runtime": {
                    "account": {"number": "DUX", "margin_usage": 0.5},
                    "option_chains": {"expirations": 4, "strikes": 10},
                },
                "symbols": {"AAA": {"weight": 1.0}},
                "overrides": {
                    "strategy_symbol": {
                        "equity_buy_rebalance": {"AAA": {"buy_only_rebalancing": True}}
                    }
                },
                "strategies": {
                    "wheel": {
                        "defaults": {
                            "target": {"dte": 30, "minimum_open_interest": 5},
                            "roll_when": {"dte": 7},
                        }
                    }
                },
            }
        )


def test_v2_uses_wheel_defaults_for_core_options_settings() -> None:
    config = Config.model_validate(
        {
            "meta": {"schema_version": 2},
            "run": {
                "stages": [{"id": "options_write_puts", "kind": "options.write_puts"}]
            },
            "runtime": {
                "account": {"number": "DUX", "margin_usage": 0.5},
                "option_chains": {"expirations": 4, "strikes": 10},
            },
            "portfolio": {"symbols": {"AAA": {"weight": 1.0}}},
            "strategies": {
                "wheel": {
                    "defaults": {
                        "target": {"dte": 30, "minimum_open_interest": 5},
                        "roll_when": {"dte": 7},
                    }
                }
            },
        }
    )
    assert config.target.dte == 30


def test_v2_rejects_top_level_defaults() -> None:
    with pytest.raises(ValueError):
        Config.model_validate(
            {
                "meta": {"schema_version": 2},
                "run": {"strategies": ["wheel"]},
                "runtime": {
                    "account": {"number": "DUX", "margin_usage": 0.5},
                    "option_chains": {"expirations": 4, "strikes": 10},
                },
                "portfolio": {"symbols": {"AAA": {"weight": 1.0}}},
                "strategies": {
                    "wheel": {
                        "defaults": {
                            "target": {"dte": 30, "minimum_open_interest": 5},
                            "roll_when": {"dte": 7},
                        }
                    }
                },
                "defaults": {"target": {"dte": 30}},
            }
        )


def test_strategy_defaults_apply_to_symbols_for_buy_rebalance() -> None:
    config = Config.model_validate(
        {
            "meta": {"schema_version": 2},
            "run": {"strategies": ["wheel"]},
            "runtime": {
                "account": {"number": "DUX", "margin_usage": 0.5},
                "option_chains": {"expirations": 4, "strikes": 10},
            },
            "portfolio": {"symbols": {"AAA": {"weight": 1.0}}},
            "strategies": {
                "wheel": {
                    "defaults": {
                        "target": {"dte": 30, "minimum_open_interest": 5},
                        "roll_when": {"dte": 7},
                    },
                    "equity_rebalance": {
                        "defaults": {"mode": "buy_only", "min_threshold_percent": 0.02}
                    },
                },
            },
        }
    )
    policy = config.wheel_rebalance_policy("AAA")
    assert policy.mode == RebalanceMode.buy_only
    assert policy.min_threshold_percent == pytest.approx(0.02)


def test_strategy_symbol_override_wins_over_defaults() -> None:
    config = Config.model_validate(
        {
            "meta": {"schema_version": 2},
            "run": {"strategies": ["wheel"]},
            "runtime": {
                "account": {"number": "DUX", "margin_usage": 0.5},
                "option_chains": {"expirations": 4, "strikes": 10},
            },
            "portfolio": {"symbols": {"AAA": {"weight": 1.0}}},
            "strategies": {
                "wheel": {
                    "defaults": {
                        "target": {"dte": 30, "minimum_open_interest": 5},
                        "roll_when": {"dte": 7},
                    },
                    "equity_rebalance": {
                        "defaults": {"mode": "buy_only", "min_threshold_percent": 0.02},
                        "symbol_overrides": {"AAA": {"min_threshold_percent": 0.05}},
                    },
                },
            },
        }
    )
    policy = config.wheel_rebalance_policy("AAA")
    assert policy.mode == RebalanceMode.buy_only
    assert policy.min_threshold_percent == pytest.approx(0.05)


def test_regime_rebalance_uses_same_equity_rebalance_policy_model() -> None:
    config = Config.model_validate(
        {
            "meta": {"schema_version": 2},
            "run": {"strategies": ["regime_rebalance"]},
            "runtime": {
                "account": {"number": "DUX", "margin_usage": 0.5},
                "option_chains": {"expirations": 4, "strikes": 10},
            },
            "portfolio": {"symbols": {"AAA": {"weight": 1.0}}},
            "strategies": {
                "wheel": {
                    "defaults": {
                        "target": {"dte": 30, "minimum_open_interest": 5},
                        "roll_when": {"dte": 7},
                    }
                },
                "regime_rebalance": {
                    "enabled": True,
                    "symbols": ["AAA"],
                    "equity_rebalance": {
                        "defaults": {"mode": "sell_only"},
                        "symbol_overrides": {
                            "AAA": {"mode": "buy_only", "min_threshold_percent": 0.03}
                        },
                    },
                },
            },
        }
    )
    policy = config.regime_rebalance_policy("AAA")
    assert policy.mode == RebalanceMode.buy_only
    assert policy.min_threshold_percent == pytest.approx(0.03)


def test_strategy_margin_usage_falls_back_to_runtime_account_margin_usage() -> None:
    config = Config(**_base_config({"strategies": ["wheel"]}))
    assert config.wheel_margin_usage() == pytest.approx(0.5)
    assert config.regime_margin_usage() == pytest.approx(0.5)


def test_strategy_margin_usage_overrides_runtime_default() -> None:
    config = Config.model_validate(
        {
            **_base_config({"strategies": ["wheel"]}),
            "strategies": {
                "wheel": {
                    "defaults": {
                        "target": {"dte": 30, "minimum_open_interest": 5},
                        "roll_when": {"dte": 7},
                    },
                    "risk": {"margin_usage": 0.35},
                },
                "regime_rebalance": {
                    "enabled": True,
                    "symbols": ["AAA"],
                    "risk": {"margin_usage": 0.8},
                },
            },
        }
    )
    assert config.wheel_margin_usage() == pytest.approx(0.35)
    assert config.regime_margin_usage() == pytest.approx(0.8)


def test_wheel_defaults_and_symbol_overrides_map_consistently() -> None:
    config = Config.model_validate(
        {
            "meta": {"schema_version": 2},
            "run": {"strategies": ["wheel"]},
            "runtime": {
                "account": {"number": "DUX", "margin_usage": 0.5},
                "option_chains": {"expirations": 4, "strikes": 10},
            },
            "portfolio": {"symbols": {"AAA": {"weight": 1.0}}},
            "strategies": {
                "wheel": {
                    "defaults": {
                        "target": {"dte": 30, "minimum_open_interest": 5},
                        "roll_when": {"dte": 7},
                        "write_calls_only_min_threshold_percent": 0.01,
                    },
                    "symbol_overrides": {
                        "AAA": {"write_calls_only_min_threshold_percent": 0.03}
                    },
                }
            },
        }
    )
    assert config.write_when.calls.min_threshold_percent == pytest.approx(0.01)
    assert config.symbols[
        "AAA"
    ].write_calls_only_min_threshold_percent == pytest.approx(0.03)


def test_v2_rejects_transitional_infrastructure_key() -> None:
    with pytest.raises(ValueError):
        Config.model_validate(
            {
                "meta": {"schema_version": 2},
                "run": {"strategies": ["wheel"]},
                "infrastructure": {"account": {"number": "DUX", "margin_usage": 0.5}},
                "portfolio": {"symbols": {"AAA": {"weight": 1.0}}},
                "strategies": {
                    "wheel": {
                        "defaults": {
                            "target": {"dte": 30, "minimum_open_interest": 5},
                            "roll_when": {"dte": 7},
                        }
                    }
                },
            }
        )
