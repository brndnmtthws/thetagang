from io import StringIO

import pytest
from rich.console import Console

from thetagang.config import (
    SHARES_ONLY_DEPRECATION_MESSAGE,
    Config,
    RebalanceMode,
    config_deprecation_warnings,
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


def _tail_target(symbol="AAA", budget_weight=1.0):
    return {"symbol": symbol, "budget_weight": budget_weight}


def test_stage_enabled_map_reflects_compiled_strategy_flags() -> None:
    config = Config(**_base_config({"strategies": ["wheel", "cash_management"]}))
    flags = stage_enabled_map(config)
    assert flags["options_write_puts"] is True
    assert flags["equity_regime_rebalance"] is False
    assert flags["post_cash_management"] is True
    assert flags["post_vix_call_hedge"] is False


def test_tail_hedge_strategy_compiles_to_its_post_stage() -> None:
    data = _base_config({"strategies": ["regime_rebalance", "tail_hedge"]})
    data["strategies"]["tail_hedge"] = {
        "enabled": True,
        "targets": [_tail_target()],
    }

    config = Config(**data)

    flags = stage_enabled_map(config)
    assert flags["equity_regime_rebalance"] is True
    assert flags["post_tail_hedge"] is True
    target = config.tail_hedge.targets[0]
    assert target.entries_per_year == 6
    assert target.minimum_entry_spacing_days == 61
    assert target.target_dte == 180
    assert target.min_dte == 120
    assert target.max_dte == 240
    assert target.exit_dte == 30
    assert not hasattr(target, "strike_ratio")
    assert target.catastrophe_drawdowns == [0.40, 0.50, 0.60]
    assert config.tail_hedge.harvest_trigger_weight == 0.05
    assert config.tail_hedge.harvest_target_weight == 0.03
    assert config.runtime.orders.estimated_fee_per_contract == 1.0


@pytest.mark.parametrize(
    "price_update_delay",
    [[30, 30], [60, 30], [-1, 30]],
)
def test_orders_reject_invalid_price_update_delay_ranges(
    price_update_delay,
) -> None:
    data = _base_config({"strategies": ["wheel"]})
    data["runtime"]["orders"] = {"price_update_delay": price_update_delay}

    with pytest.raises(ValueError, match="price_update_delay"):
        Config(**data)


def test_tail_hedge_rejects_removed_strike_ratio() -> None:
    data = _base_config({"strategies": ["tail_hedge"]})
    data["strategies"]["tail_hedge"] = {
        "enabled": True,
        "targets": [{**_tail_target(), "strike_ratio": 0.60}],
    }

    with pytest.raises(ValueError, match="strike_ratio"):
        Config(**data)


@pytest.mark.parametrize(
    "drawdowns",
    [[0.50, 0.40], [0.40, 0.40], [0.0, 0.50], [0.50, 1.0]],
)
def test_tail_hedge_catastrophe_drawdowns_are_ordered_fractions(
    drawdowns,
) -> None:
    data = _base_config({"strategies": ["tail_hedge"]})
    data["strategies"]["tail_hedge"] = {
        "enabled": True,
        "targets": [
            {
                **_tail_target(),
                "catastrophe_drawdowns": drawdowns,
            }
        ],
    }

    with pytest.raises(ValueError, match="catastrophe_drawdowns"):
        Config(**data)


@pytest.mark.parametrize("target", [0.05, 0.06])
def test_tail_hedge_harvest_target_must_be_below_trigger(target) -> None:
    data = _base_config({"strategies": ["tail_hedge"]})
    data["strategies"]["tail_hedge"] = {
        "enabled": True,
        "harvest_trigger_weight": 0.05,
        "harvest_target_weight": target,
        "targets": [_tail_target()],
    }

    with pytest.raises(ValueError, match="harvest_target_weight"):
        Config(**data)


def test_enabled_tail_hedge_requires_sqlite_state() -> None:
    data = _base_config({"strategies": ["tail_hedge"]})
    data["runtime"]["database"] = {"enabled": False}
    data["strategies"]["tail_hedge"] = {
        "enabled": True,
        "targets": [_tail_target()],
    }

    with pytest.raises(ValueError, match="targets require runtime.database.enabled"):
        Config(**data)


def test_retained_targets_require_state_even_while_tail_hedge_is_disabled() -> None:
    data = _base_config({"strategies": ["wheel"]})
    data["runtime"]["database"] = {"enabled": False}
    data["strategies"]["tail_hedge"] = {
        "enabled": False,
        "targets": [_tail_target()],
    }

    with pytest.raises(ValueError, match="targets require runtime.database.enabled"):
        Config(**data)


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite:///:memory:",
        "sqlite:///file:shared?mode=memory&cache=shared&uri=true",
        "sqlite+aiosqlite:///state.db",
    ],
)
def test_enabled_tail_hedge_requires_persistent_sqlite_state(
    database_url: str,
) -> None:
    data = _base_config({"strategies": ["tail_hedge"]})
    data["runtime"]["database"] = {"enabled": True, "url": database_url}
    data["strategies"]["tail_hedge"] = {
        "enabled": True,
        "targets": [_tail_target()],
    }

    with pytest.raises(ValueError, match="supported file-backed SQLite URL"):
        Config(**data)


@pytest.mark.parametrize("driver", ["sqlite", "sqlite+pysqlite"])
def test_enabled_tail_hedge_accepts_file_backed_sqlite_state(
    tmp_path,
    driver: str,
) -> None:
    data = _base_config({"strategies": ["tail_hedge"]})
    data["runtime"]["database"] = {
        "enabled": True,
        "url": f"{driver}:///{tmp_path / 'state.db'}",
    }
    data["strategies"]["tail_hedge"] = {
        "enabled": True,
        "targets": [_tail_target()],
    }

    assert Config(**data).tail_hedge.enabled is True


def test_enabled_tail_hedge_accepts_blank_database_url_path_fallback() -> None:
    data = _base_config({"strategies": ["tail_hedge"]})
    data["runtime"]["database"] = {
        "enabled": True,
        "path": "data/tail-state.db",
        "url": "",
    }
    data["strategies"]["tail_hedge"] = {
        "enabled": True,
        "targets": [_tail_target()],
    }

    config = Config(**data)

    assert config.tail_hedge.enabled is True
    resolved_url = config.runtime.database.resolve_url("/tmp/thetagang.toml")
    assert resolved_url.startswith("sqlite:///")
    assert resolved_url.endswith("/data/tail-state.db")


def test_enabled_tail_hedge_requires_a_managed_symbol() -> None:
    data = _base_config({"strategies": ["tail_hedge"]})
    data["strategies"]["tail_hedge"] = {
        "enabled": True,
        "targets": [_tail_target("ZZZ")],
    }

    with pytest.raises(ValueError, match="target symbols must be in portfolio.symbols"):
        Config(**data)


def test_tail_hedge_accepts_deprecated_shares_only_regime_setting() -> None:
    data = _base_config({"strategies": ["regime_rebalance", "tail_hedge"]})
    data["strategies"]["regime_rebalance"] = {
        "enabled": True,
        "shares_only": True,
    }
    data["strategies"]["tail_hedge"] = {
        "enabled": True,
        "targets": [_tail_target()],
    }

    config = Config(**data)

    flags = stage_enabled_map(config)
    assert flags["equity_regime_rebalance"] is True
    assert flags["post_tail_hedge"] is True


@pytest.mark.parametrize("value", [True, False])
def test_explicit_shares_only_setting_emits_deprecation_message(value: bool) -> None:
    data = _base_config({"strategies": ["regime_rebalance"]})
    data["strategies"]["regime_rebalance"] = {"shares_only": value}

    warnings = config_deprecation_warnings(data)

    assert warnings == [SHARES_ONLY_DEPRECATION_MESSAGE]


def test_trading_capabilities_are_derived_from_resolved_stages() -> None:
    data = _base_config(
        {"strategies": ["regime_rebalance", "tail_hedge", "cash_management"]}
    )
    config = Config(**data)
    output = StringIO()
    console = Console(file=output, width=140, color_system=None)

    console.print(config.create_trading_capabilities_table())

    rendered = output.getvalue()
    assert "Regime share rebalancing" in rendered
    assert "Tail-hedge option trading" in rendered
    assert "Cash management" in rendered
    assert "Wheel put writing" in rendered
    assert "equity_regime_rebalance" in rendered
    assert "post_tail_hedge" in rendered
    assert rendered.count("Enabled") == 3


def test_tail_hedge_accepts_an_empty_desired_target_set_for_cleanup() -> None:
    data = _base_config({"strategies": ["tail_hedge"]})
    data["strategies"]["tail_hedge"] = {"enabled": True}

    config = Config(**data)

    assert config.tail_hedge.targets == []
    assert stage_enabled_map(config)["post_tail_hedge"] is True


def test_tail_hedge_targets_require_unique_symbols_and_complete_budget() -> None:
    data = _base_config({"strategies": ["tail_hedge"]})
    data["portfolio"]["symbols"]["BBB"] = {"weight": 0.0}
    data["strategies"]["tail_hedge"] = {
        "enabled": True,
        "targets": [_tail_target("AAA", 0.5), _tail_target("BBB", 0.5)],
    }

    config = Config(**data)
    assert [target.symbol for target in config.tail_hedge.targets] == ["AAA", "BBB"]

    data["strategies"]["tail_hedge"]["targets"][1]["symbol"] = "AAA"
    with pytest.raises(ValueError, match="symbols must be unique"):
        Config(**data)

    data["strategies"]["tail_hedge"]["targets"][1] = _tail_target("BBB", 0.4)
    with pytest.raises(ValueError, match="budget_weight values must sum to 1"):
        Config(**data)


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


@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        ("equity_regime_rebalance", "post_tail_hedge"),
        ("equity_buy_rebalance", "post_tail_hedge"),
        ("equity_sell_rebalance", "post_tail_hedge"),
        ("equity_regime_rebalance", "post_cash_management"),
        ("post_tail_hedge", "post_cash_management"),
    ],
)
def test_explicit_run_stages_reject_unsafe_tail_and_cash_ordering(
    earlier: str,
    later: str,
) -> None:
    stage_kinds = {
        "equity_regime_rebalance": "equity.regime_rebalance",
        "equity_buy_rebalance": "equity.buy_rebalance",
        "equity_sell_rebalance": "equity.sell_rebalance",
        "post_tail_hedge": "post.tail_hedge",
        "post_cash_management": "post.cash_management",
    }
    with pytest.raises(
        ValueError,
        match=rf"{earlier} must appear before run\.stages\.{later}",
    ):
        Config(
            **_base_config(
                {
                    "stages": [
                        {
                            "id": later,
                            "kind": stage_kinds[later],
                            "enabled": True,
                        },
                        {
                            "id": earlier,
                            "kind": stage_kinds[earlier],
                            "enabled": True,
                        },
                    ]
                }
            )
        )


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


def test_explicit_run_config_rejects_calls_stage_without_puts_stage() -> None:
    with pytest.raises(
        ValueError, match="options_write_calls requires enabled stage\\(s\\)"
    ):
        Config(
            **_base_config(
                {
                    "stages": [
                        {
                            "id": "options_write_calls",
                            "kind": "options.write_calls",
                            "enabled": True,
                        }
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


def test_symbol_accepts_volatility_weight_config() -> None:
    data = _base_config({"strategies": ["wheel"]})
    data["portfolio"]["symbols"]["AAA"]["volatility_weight"] = {
        "enabled": True,
        "target_vol": 0.32,
        "lookback_days": 30,
        "min_weight": 0.25,
        "max_weight": 1.0,
        "rebalance_band": 0.05,
    }

    config = Config(**data)

    volatility_weight = config.portfolio.symbols["AAA"].volatility_weight
    assert volatility_weight is not None
    assert volatility_weight.enabled is True
    assert volatility_weight.target_vol == 0.32
    assert volatility_weight.smoothing_factor == 0.3


def test_symbol_accepts_volatility_weight_above_base_weight() -> None:
    data = _base_config({"strategies": ["wheel"]})
    data["portfolio"]["symbols"]["AAA"]["weight"] = 0.4
    data["portfolio"]["symbols"]["BBB"] = {"weight": 0.6}
    data["portfolio"]["symbols"]["AAA"]["volatility_weight"] = {
        "enabled": True,
        "target_vol": 0.32,
        "lookback_days": 30,
        "min_weight": 0.25,
        "max_weight": 0.5,
    }

    config = Config(**data)

    volatility_weight = config.portfolio.symbols["AAA"].volatility_weight
    assert volatility_weight is not None
    assert volatility_weight.max_weight == 0.5


def test_symbol_accepts_absolute_trend_config() -> None:
    data = _base_config({"strategies": ["wheel"]})
    data["portfolio"]["symbols"]["AAA"]["absolute_trend"] = {
        "enabled": True,
        "lookback_days": 168,
        "risk_off_multiplier": 0.15,
    }

    config = Config(**data)

    absolute_trend = config.portfolio.symbols["AAA"].absolute_trend
    assert absolute_trend is not None
    assert absolute_trend.enabled is True
    assert absolute_trend.lookback_days == 168
    assert absolute_trend.risk_off_multiplier == pytest.approx(0.15)


def test_symbol_absolute_trend_defaults_disabled() -> None:
    data = _base_config({"strategies": ["wheel"]})
    data["portfolio"]["symbols"]["AAA"]["absolute_trend"] = {}

    config = Config(**data)

    absolute_trend = config.portfolio.symbols["AAA"].absolute_trend
    assert absolute_trend is not None
    assert absolute_trend.enabled is False
    assert absolute_trend.lookback_days == 168
    assert absolute_trend.risk_off_multiplier == pytest.approx(0.15)


def test_portfolio_configured_weights_must_sum_to_100() -> None:
    data = _base_config({"strategies": ["wheel"]})
    data["portfolio"]["symbols"] = {
        "AAA": {"weight": 0.65},
        "BBB": {"weight": 0.40},
    }

    with pytest.raises(ValueError, match="Symbol weights must sum to 1.0"):
        Config(**data)


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


def test_wheel_symbol_overrides_reject_invalid_types() -> None:
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
                        },
                        "symbol_overrides": {
                            "AAA": {
                                "write_calls_only_min_threshold_percent": "not-a-float"
                            }
                        },
                    }
                },
            }
        )


def test_wheel_symbol_overrides_reject_unknown_keys() -> None:
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
                        },
                        "symbol_overrides": {"AAA": {"unexpected_field": 1}},
                    }
                },
            }
        )


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
