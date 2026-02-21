from types import SimpleNamespace

import pytest

from thetagang.strategies.runtime_services import resolve_symbol_configs


def test_resolve_symbol_configs_prefers_config_symbols() -> None:
    config = SimpleNamespace(
        symbols={"AAA": SimpleNamespace(weight=1.0)},
        portfolio=SimpleNamespace(symbols={"BBB": SimpleNamespace(weight=1.0)}),
    )

    resolved = resolve_symbol_configs(config, context="test")
    assert list(resolved.keys()) == ["AAA"]


def test_resolve_symbol_configs_falls_back_to_portfolio_symbols() -> None:
    config = SimpleNamespace(
        symbols=None,
        portfolio=SimpleNamespace(symbols={"BBB": SimpleNamespace(weight=1.0)}),
    )

    resolved = resolve_symbol_configs(config, context="test")
    assert list(resolved.keys()) == ["BBB"]


def test_resolve_symbol_configs_raises_for_invalid_shape() -> None:
    config = SimpleNamespace(symbols=None, portfolio=SimpleNamespace(symbols=None))

    with pytest.raises(ValueError, match="expected config.symbols"):
        resolve_symbol_configs(config, context="runtime-check")
