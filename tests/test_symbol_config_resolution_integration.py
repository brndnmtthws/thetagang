from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import cast

import pytest

from thetagang.config import Config
from thetagang.strategies.equity_engine import (
    EquityRebalanceEngine,
    EquityRuntimeServices,
)
from thetagang.strategies.options_engine import (
    OptionsRuntimeServices,
    OptionsStrategyEngine,
)
from thetagang.strategies.regime_engine import RegimeRebalanceEngine


@pytest.mark.asyncio
async def test_equity_engine_fails_fast_for_invalid_symbol_config_shape(mocker) -> None:
    config = SimpleNamespace(
        symbols=None,
        portfolio=SimpleNamespace(symbols=None),
        regime_rebalance=SimpleNamespace(enabled=False, symbols=[]),
        is_buy_only_rebalancing=lambda _symbol: True,
    )
    services = SimpleNamespace(
        get_primary_exchange=lambda _symbol: "SMART",
        get_buying_power=lambda _account_summary: 0,
        midpoint_or_market_price=lambda _ticker: 0.0,
    )
    engine = EquityRebalanceEngine(
        config=cast(Config, config),
        ibkr=mocker.Mock(),
        order_ops=mocker.Mock(),
        services=cast(EquityRuntimeServices, services),
        regime_engine=mocker.Mock(),
    )

    with pytest.raises(ValueError, match="buy-only rebalancing"):
        await engine.check_buy_only_positions({}, {})


@pytest.mark.asyncio
async def test_options_engine_fails_fast_for_invalid_symbol_config_shape(
    mocker,
) -> None:
    config = SimpleNamespace(
        symbols=None,
        portfolio=SimpleNamespace(symbols=None),
        strategies=SimpleNamespace(
            wheel=SimpleNamespace(
                defaults=SimpleNamespace(
                    write_when=SimpleNamespace(calculate_net_contracts=False)
                )
            )
        ),
    )
    services = SimpleNamespace(
        get_symbols=lambda: [],
        get_primary_exchange=lambda _symbol: "SMART",
        get_buying_power=lambda _account_summary: 0,
        get_maximum_new_contracts_for=lambda *_args, **_kwargs: 0,
        get_write_threshold=lambda *_args, **_kwargs: (0.0, 0.0),
        get_close_price=lambda _ticker: 0.0,
    )
    engine = OptionsStrategyEngine(
        config=cast(Config, config),
        ibkr=mocker.Mock(),
        option_scanner=mocker.Mock(),
        order_ops=mocker.Mock(),
        services=cast(OptionsRuntimeServices, services),
        target_quantities={},
        has_excess_puts=set(),
        has_excess_calls=set(),
        qualified_contracts={},
    )

    with pytest.raises(ValueError, match="options put write check"):
        await engine.check_if_can_write_puts({}, {})


@pytest.mark.asyncio
async def test_regime_engine_fails_fast_for_invalid_symbol_config_shape(mocker) -> None:
    config = SimpleNamespace(
        symbols=None,
        portfolio=SimpleNamespace(symbols=None),
        strategies=SimpleNamespace(
            regime_rebalance=SimpleNamespace(enabled=False, symbols=[])
        ),
    )
    engine = RegimeRebalanceEngine(
        config=cast(Config, config),
        ibkr=mocker.Mock(),
        order_ops=mocker.Mock(),
        data_store=None,
        get_primary_exchange=lambda _symbol: "SMART",
        get_buying_power=lambda _account_summary: 0,
        now_provider=lambda: datetime.now(),
    )

    with pytest.raises(ValueError, match="regime rebalance check"):
        await engine.check_regime_rebalance_positions({}, {})
