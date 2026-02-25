from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List

import tomlkit

from thetagang.config import DEFAULT_RUN_STRATEGIES
from thetagang.legacy_config import LegacyConfig, normalize_config

RUNTIME_KEYS = [
    "account",
    "option_chains",
    "exchange_hours",
    "orders",
    "database",
    "ib_async",
    "ibc",
    "watchdog",
]
DEFAULT_KEYS = ["target", "write_when", "roll_when", "constants"]

BUY_REBALANCE_SYMBOL_KEYS = [
    "buy_only_rebalancing",
    "buy_only_min_threshold_shares",
    "buy_only_min_threshold_amount",
    "buy_only_min_threshold_percent",
    "buy_only_min_threshold_percent_relative",
]
SELL_REBALANCE_SYMBOL_KEYS = [
    "sell_only_rebalancing",
    "sell_only_min_threshold_shares",
    "sell_only_min_threshold_amount",
    "sell_only_min_threshold_percent",
    "sell_only_min_threshold_percent_relative",
]
WHEEL_SYMBOL_OVERRIDE_KEYS = [
    "write_calls_only_min_threshold_percent",
    "write_calls_only_min_threshold_percent_relative",
]
POLICY_KEY_MAP = {
    "buy_only_min_threshold_shares": "min_threshold_shares",
    "sell_only_min_threshold_shares": "min_threshold_shares",
    "buy_only_min_threshold_amount": "min_threshold_amount",
    "sell_only_min_threshold_amount": "min_threshold_amount",
    "buy_only_min_threshold_percent": "min_threshold_percent",
    "sell_only_min_threshold_percent": "min_threshold_percent",
    "buy_only_min_threshold_percent_relative": "min_threshold_percent_relative",
    "sell_only_min_threshold_percent_relative": "min_threshold_percent_relative",
}


@dataclass
class MappingEntry:
    old_path: str
    new_path: str
    note: str = ""


@dataclass
class MigrationResult:
    source_schema: str
    target_schema: str
    migrated_text: str
    mappings: List[MappingEntry] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def migrate_v1_to_v2(raw_text: str) -> MigrationResult:
    source_doc = tomlkit.parse(raw_text)
    source_data: Dict[str, Any] = source_doc.unwrap()
    normalized_legacy = normalize_config(copy.deepcopy(source_data))
    legacy_model = LegacyConfig.model_validate(normalized_legacy)
    validated_legacy = legacy_model.model_dump(mode="python")
    mappings: List[MappingEntry] = []
    warnings: List[str] = []

    runtime: Dict[str, Any] = {}
    wheel_defaults: Dict[str, Any] = {}
    symbols_section: Dict[str, Any] = {}
    strategies: Dict[str, Any] = {}
    wheel_rebalance_symbol_overrides: Dict[str, Any] = {}
    wheel_symbol_overrides: Dict[str, Any] = {}

    for key in RUNTIME_KEYS:
        if _key_present_in_source(source_data, key):
            runtime[key] = _without_none(validated_legacy[key])
            mappings.append(MappingEntry(key, f"runtime.{key}"))

    for key in DEFAULT_KEYS:
        if _key_present_in_source(source_data, key):
            wheel_defaults[key] = _without_none(validated_legacy[key])
            mappings.append(MappingEntry(key, f"strategies.wheel.defaults.{key}"))

    if "symbols" in validated_legacy:
        symbols_section = _without_none(validated_legacy["symbols"])
        mappings.append(MappingEntry("symbols", "portfolio.symbols"))

    strategy_map = {
        "regime_rebalance": "regime_rebalance",
        "vix_call_hedge": "vix_call_hedge",
        "cash_management": "cash_management",
    }
    for old_key, new_key in strategy_map.items():
        if _key_present_in_source(source_data, old_key):
            strategies[new_key] = _without_none(validated_legacy[old_key])
            mappings.append(MappingEntry(old_key, f"strategies.{new_key}"))

    symbols = validated_legacy.get("symbols", {})
    for symbol, values in symbols.items():
        if not isinstance(values, dict):
            continue
        policy_override: Dict[str, Any] = {}
        buy_only_enabled = bool(values.get("buy_only_rebalancing", False))
        sell_only_enabled = bool(values.get("sell_only_rebalancing", False))
        if buy_only_enabled and sell_only_enabled:
            policy_override["mode"] = "both"
        elif buy_only_enabled:
            policy_override["mode"] = "buy_only"
        elif sell_only_enabled:
            policy_override["mode"] = "sell_only"

        threshold_sources: Dict[str, List[Any]] = {}
        for old_key, new_key in POLICY_KEY_MAP.items():
            val = values.get(old_key)
            if val is None:
                continue
            threshold_sources.setdefault(new_key, []).append(val)

        for key, vals in threshold_sources.items():
            unique_vals = list(dict.fromkeys(vals))
            if len(unique_vals) > 1:
                warnings.append(
                    f"{symbol}: buy/sell threshold conflict for {key}; using max({unique_vals})."
                )
                policy_override[key] = max(unique_vals)
            else:
                policy_override[key] = unique_vals[0]

        if policy_override:
            wheel_rebalance_symbol_overrides[symbol] = policy_override
            mappings.append(
                MappingEntry(
                    f"symbols.{symbol}.buy_only_*/sell_only_*",
                    f"strategies.wheel.equity_rebalance.symbol_overrides.{symbol}",
                )
            )

        wheel_keys = {k: values[k] for k in WHEEL_SYMBOL_OVERRIDE_KEYS if k in values}
        wheel_keys = _without_none(wheel_keys)
        if wheel_keys:
            wheel_symbol_overrides[symbol] = wheel_keys
            mappings.append(
                MappingEntry(
                    f"symbols.{symbol}.write_calls_only_*",
                    f"strategies.wheel.symbol_overrides.{symbol}",
                )
            )

    if wheel_defaults:
        strategies["wheel"] = {"defaults": wheel_defaults}
        write_when = wheel_defaults.get("write_when", {})
        if isinstance(write_when, dict):
            calls = write_when.get("calls", {})
            if isinstance(calls, dict):
                for old_key, new_key in (
                    (
                        "min_threshold_percent",
                        "write_calls_only_min_threshold_percent",
                    ),
                    (
                        "min_threshold_percent_relative",
                        "write_calls_only_min_threshold_percent_relative",
                    ),
                ):
                    if old_key in calls and new_key not in wheel_defaults:
                        wheel_defaults[new_key] = calls[old_key]
                        mappings.append(
                            MappingEntry(
                                f"write_when.calls.{old_key}",
                                f"strategies.wheel.defaults.{new_key}",
                            )
                        )
    if wheel_rebalance_symbol_overrides:
        strategies.setdefault("wheel", {})
        strategies["wheel"]["equity_rebalance"] = {
            "symbol_overrides": wheel_rebalance_symbol_overrides
        }
    if wheel_symbol_overrides:
        strategies.setdefault("wheel", {})
        strategies["wheel"]["symbol_overrides"] = wheel_symbol_overrides

    run_plan = _infer_run_plan(validated_legacy, warnings)
    v2_doc = tomlkit.document()
    v2_doc.add("meta", tomlkit.item({"schema_version": 2}))
    v2_doc.add("run", tomlkit.item(run_plan))

    infra_doc = tomlkit.table()
    for key in RUNTIME_KEYS:
        if key in runtime:
            source_item = _source_section(source_doc, key)
            if source_item is not None:
                infra_doc.add(key, source_item)
            else:
                infra_doc.add(key, tomlkit.item(runtime[key]))
    v2_doc.add("runtime", infra_doc)

    symbols_doc = _symbols_section(source_doc, source_data, symbols_section)
    if wheel_rebalance_symbol_overrides or wheel_symbol_overrides:
        _strip_legacy_symbol_strategy_keys(symbols_doc)
    portfolio_doc = tomlkit.table()
    portfolio_doc.add("symbols", symbols_doc)
    v2_doc.add("portfolio", portfolio_doc)

    strategies_doc = tomlkit.table()
    if "wheel" in strategies:
        wheel_doc = tomlkit.table()
        wheel_defaults_doc = tomlkit.table()
        for key in DEFAULT_KEYS:
            if key in wheel_defaults:
                source_item = _source_section(source_doc, key)
                if source_item is not None:
                    wheel_defaults_doc.add(key, source_item)
                else:
                    wheel_defaults_doc.add(key, tomlkit.item(wheel_defaults[key]))
        for key in sorted(k for k in wheel_defaults if k not in DEFAULT_KEYS):
            wheel_defaults_doc.add(key, tomlkit.item(wheel_defaults[key]))
        wheel_doc.add("defaults", wheel_defaults_doc)
        if wheel_rebalance_symbol_overrides:
            wheel_doc.add(
                "equity_rebalance",
                tomlkit.item({"symbol_overrides": wheel_rebalance_symbol_overrides}),
            )
        if wheel_symbol_overrides:
            wheel_doc.add("symbol_overrides", tomlkit.item(wheel_symbol_overrides))
        strategies_doc.add("wheel", wheel_doc)

    for old_key, new_key in strategy_map.items():
        if new_key in strategies:
            source_item = _source_section(source_doc, old_key)
            if source_item is not None:
                strategies_doc.add(new_key, source_item)
            else:
                strategies_doc.add(new_key, tomlkit.item(strategies[new_key]))

    v2_doc.add("strategies", strategies_doc)

    for required in ["account", "symbols", "target", "roll_when", "option_chains"]:
        if required not in source_data:
            warnings.append(f"Missing expected legacy section: {required}")

    return MigrationResult(
        source_schema="v1",
        target_schema="v2",
        migrated_text=_serialize(v2_doc),
        mappings=mappings,
        warnings=warnings,
    )


def _serialize(data: Any) -> str:
    # deterministic output
    return tomlkit.dumps(data)


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_none(v) for v in value]
    return value


def _source_section(source_doc: Any, key: str) -> Any:
    if key not in source_doc:
        return None
    return copy.deepcopy(source_doc[key])


def _key_present_in_source(source_data: Dict[str, Any], key: str) -> bool:
    if key in source_data:
        return True
    # Legacy key that normalize_config upgrades to ib_async.
    return key == "ib_async" and "ib_insync" in source_data


def _symbols_section(
    source_doc: Any, source_data: Dict[str, Any], symbols: Dict[str, Any]
) -> Any:
    source_symbols = source_data.get("symbols", {})
    has_parts = isinstance(source_symbols, dict) and any(
        isinstance(v, dict) and "parts" in v for v in source_symbols.values()
    )
    if "symbols" in source_doc:
        symbols_doc = copy.deepcopy(source_doc["symbols"])
        if has_parts:
            _rewrite_parts_to_weight(symbols_doc, symbols)
        return symbols_doc
    return tomlkit.item(symbols)


def _rewrite_parts_to_weight(
    symbols_doc: Any, normalized_symbols: Dict[str, Any]
) -> None:
    if not hasattr(symbols_doc, "items"):
        return
    for symbol, symbol_cfg in symbols_doc.items():
        if not hasattr(symbol_cfg, "__contains__"):
            continue
        if "parts" not in symbol_cfg:
            continue
        del symbol_cfg["parts"]
        normalized = normalized_symbols.get(symbol, {})
        if isinstance(normalized, dict) and "weight" in normalized:
            symbol_cfg["weight"] = normalized["weight"]


def _strip_legacy_symbol_strategy_keys(symbols_doc: Any) -> None:
    if not hasattr(symbols_doc, "items"):
        return
    for _symbol, symbol_cfg in symbols_doc.items():
        if not hasattr(symbol_cfg, "__contains__"):
            continue
        for key in (
            BUY_REBALANCE_SYMBOL_KEYS
            + SELL_REBALANCE_SYMBOL_KEYS
            + WHEEL_SYMBOL_OVERRIDE_KEYS
        ):
            if key in symbol_cfg:
                del symbol_cfg[key]


def _infer_run_plan(
    validated_legacy: Dict[str, Any], _warnings: List[str]
) -> Dict[str, Any]:
    return {"strategies": _infer_run_strategies(validated_legacy)}


def _infer_run_strategies(validated_legacy: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    regime = validated_legacy.get("regime_rebalance", {})
    regime_enabled = (
        bool(regime.get("enabled", False)) if isinstance(regime, dict) else False
    )
    if regime_enabled:
        out.append("regime_rebalance")
    wheel_enabled = not regime_enabled
    if wheel_enabled:
        out.append("wheel")

    vix = validated_legacy.get("vix_call_hedge", {})
    if isinstance(vix, dict) and bool(vix.get("enabled", False)):
        out.append("vix_call_hedge")

    cash = validated_legacy.get("cash_management", {})
    if isinstance(cash, dict) and bool(cash.get("enabled", False)):
        out.append("cash_management")

    explicitly_enabled = [strategy_id for strategy_id in out if strategy_id != "wheel"]
    if explicitly_enabled:
        return explicitly_enabled
    return list(DEFAULT_RUN_STRATEGIES)
