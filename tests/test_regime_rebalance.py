import math
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from ib_async import IB, Option, Stock

import thetagang.portfolio_manager as pm_module
import thetagang.strategies.regime_engine as regime_engine_module
from thetagang.config import Config
from thetagang.db import DataStore
from thetagang.legacy_config import (
    RatioGateConfig,
    RegimeRebalanceBaseEnum,
    RegimeRebalanceConfig,
    normalize_config,
)
from thetagang.portfolio_manager import PortfolioManager
from thetagang.strategies.regime_engine import (
    REGIME_HISTORY_MAX_ATTEMPTS,
    REGIME_HISTORY_TIMEFRAME,
)
from thetagang.strategies.tail_hedge_engine import (
    TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX,
    TAIL_HEDGE_STATE_EVENT,
    TAIL_HEDGE_STATE_SCHEMA_VERSION,
)

REGIME_HISTORY_START = datetime(2024, 1, 2)
REGIME_SYMBOLS = ("AAA", "BBB")


@pytest.fixture
def mock_ib(mocker):
    mock = mocker.Mock(spec=IB)
    mock.orderStatusEvent = mocker.Mock()
    mock.orderStatusEvent.__iadd__ = mocker.Mock(return_value=None)
    return mock


@pytest.fixture
def portfolio_manager(mock_ib, mocker):
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            account=SimpleNamespace(number="TEST123", margin_usage=1.0),
            ib_async=SimpleNamespace(api_response_wait_time=1),
            orders=SimpleNamespace(
                exchange="SMART",
                algo=SimpleNamespace(strategy="Adaptive", params=[]),
            ),
            exchange_hours=SimpleNamespace(exchange="XNYS"),
        ),
        trading_is_allowed=mocker.Mock(return_value=True),
        portfolio=SimpleNamespace(
            symbols={
                "AAA": SimpleNamespace(weight=0.5, primary_exchange="NYSE"),
                "BBB": SimpleNamespace(weight=0.5, primary_exchange="NYSE"),
            }
        ),
        strategies=SimpleNamespace(
            cash_management=SimpleNamespace(cash_fund="SHV"),
            tail_hedge=SimpleNamespace(enabled=False, targets=[]),
            regime_rebalance=SimpleNamespace(
                enabled=True,
                symbols=["AAA", "BBB"],
                lookback_days=3,
                soft_band=0.10,
                hard_band=0.80,
                hard_band_rebalance_fraction=1.0,
                cooldown_days=2,
                choppiness_min=0.1,
                efficiency_max=0.9,
                flow_trade_min=0.025,
                flow_trade_stop=0.0125,
                flow_imbalance_tau=0.7,
                deficit_rail_start=0.06,
                deficit_rail_stop=0.03,
                eps=1e-8,
                order_history_lookback_days=30,
                shares_only=False,
                weight_base=RegimeRebalanceBaseEnum.net_liq_ex_options,
            ),
        ),
    )

    completion_future = mocker.Mock()
    return PortfolioManager(
        cast(Config, config), mock_ib, completion_future, dry_run=False
    )


@pytest.fixture
def portfolio_manager_with_db(mock_ib, mocker, tmp_path):
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            account=SimpleNamespace(number="TEST123", margin_usage=1.0),
            ib_async=SimpleNamespace(api_response_wait_time=1),
            orders=SimpleNamespace(
                exchange="SMART",
                algo=SimpleNamespace(strategy="Adaptive", params=[]),
            ),
            exchange_hours=SimpleNamespace(exchange="XNYS"),
        ),
        trading_is_allowed=mocker.Mock(return_value=True),
        portfolio=SimpleNamespace(
            symbols={
                "AAA": SimpleNamespace(weight=0.5, primary_exchange="NYSE"),
                "BBB": SimpleNamespace(weight=0.5, primary_exchange="NYSE"),
            }
        ),
        strategies=SimpleNamespace(
            cash_management=SimpleNamespace(cash_fund="SHV"),
            tail_hedge=SimpleNamespace(enabled=False, targets=[]),
            regime_rebalance=SimpleNamespace(
                enabled=True,
                symbols=["AAA", "BBB"],
                lookback_days=3,
                soft_band=0.10,
                hard_band=0.80,
                hard_band_rebalance_fraction=1.0,
                cooldown_days=2,
                choppiness_min=0.1,
                efficiency_max=0.9,
                flow_trade_min=0.025,
                flow_trade_stop=0.0125,
                flow_imbalance_tau=0.7,
                deficit_rail_start=0.06,
                deficit_rail_stop=0.03,
                eps=1e-8,
                order_history_lookback_days=30,
                shares_only=False,
                weight_base=RegimeRebalanceBaseEnum.net_liq_ex_options,
            ),
        ),
    )

    data_store = DataStore(
        f"sqlite:///{tmp_path / 'state.db'}",
        str(tmp_path / "thetagang.toml"),
        dry_run=False,
        config_text="test",
    )

    completion_future = mocker.Mock()
    return PortfolioManager(
        cast(Config, config),
        mock_ib,
        completion_future,
        dry_run=False,
        data_store=data_store,
    )


def _freeze_now(monkeypatch, fixed: datetime) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz:
                return cls.fromtimestamp(fixed.timestamp(), tz)
            return cls(
                fixed.year,
                fixed.month,
                fixed.day,
                fixed.hour,
                fixed.minute,
                fixed.second,
                fixed.microsecond,
            )

    monkeypatch.setattr(pm_module, "datetime", FixedDatetime)


def _mock_regime_history(portfolio_manager, mocker, closes):
    bars = _regime_bars(closes)
    _mock_required_regime_history_dates(portfolio_manager, mocker)

    async def _get_history(*_args, **_kwargs):
        return bars

    portfolio_manager.ibkr.request_historical_data = mocker.AsyncMock(
        side_effect=_get_history
    )
    return bars


def _regime_bars(closes, start_date: datetime = REGIME_HISTORY_START):
    return [
        SimpleNamespace(date=start_date + timedelta(days=offset), close=close)
        for offset, close in enumerate(closes)
    ]


def _mock_regime_histories(portfolio_manager, mocker, closes_by_symbol):
    bars_by_symbol = {
        symbol: _regime_bars(closes) for symbol, closes in closes_by_symbol.items()
    }
    _mock_required_regime_history_dates(portfolio_manager, mocker)

    async def _get_history(contract, *_args, **_kwargs):
        return bars_by_symbol[contract.symbol]

    portfolio_manager.ibkr.request_historical_data = mocker.AsyncMock(
        side_effect=_get_history
    )
    return bars_by_symbol


def _mock_regime_tickers(
    portfolio_manager,
    mocker,
    aaa_price=100.0,
    bbb_price=100.0,
    extra_prices: dict[str, float] | None = None,
):
    aaa_ticker = mocker.Mock()
    aaa_ticker.marketPrice.return_value = aaa_price
    bbb_ticker = mocker.Mock()
    bbb_ticker.marketPrice.return_value = bbb_price
    tickers = {"AAA": aaa_ticker, "BBB": bbb_ticker}
    for symbol, price in (extra_prices or {}).items():
        ticker = mocker.Mock()
        ticker.marketPrice.return_value = price
        tickers[symbol] = ticker

    async def _get_ticker(symbol, _primary_exchange):
        return tickers[symbol]

    portfolio_manager.ibkr.get_ticker_for_stock = mocker.AsyncMock(
        side_effect=_get_ticker
    )


def _mock_regime_broker(portfolio_manager, mocker, **history_mock_kwargs) -> None:
    _mock_regime_tickers(portfolio_manager, mocker)
    portfolio_manager.ibkr.request_historical_data = mocker.AsyncMock(
        **history_mock_kwargs
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])


def _stock_position(symbol: str, position: int, market_value: float | None = None):
    return SimpleNamespace(
        contract=Stock(symbol, "SMART", "USD"),
        position=position,
        marketValue=market_value,
    )


def _regime_account_summary(value: str = "400", *, cash: str | None = None):
    summary = {"NetLiquidation": SimpleNamespace(value=value)}
    if cash is not None:
        summary["TotalCashValue"] = SimpleNamespace(value=cash)
    return summary


def _regime_stock_positions(aaa: int = 3, bbb: int = 1):
    return {
        "AAA": [_stock_position("AAA", aaa)],
        "BBB": [_stock_position("BBB", bbb)],
    }


def _expected_regime_history_fetches(symbols=REGIME_SYMBOLS) -> int:
    return len(symbols) * REGIME_HISTORY_MAX_ATTEMPTS


def _disable_regime_history_retry_delay(monkeypatch) -> None:
    monkeypatch.setattr(
        "thetagang.strategies.regime_engine.REGIME_HISTORY_RETRY_DELAY_SECONDS",
        0.0,
    )


def _required_regime_history_dates(required_points: int) -> list[date]:
    return [
        (REGIME_HISTORY_START + timedelta(days=offset)).date()
        for offset in range(required_points)
    ]


def _set_required_regime_history_dates(
    portfolio_manager, monkeypatch, required_points: int
) -> list[date]:
    required_dates = _required_regime_history_dates(required_points)
    monkeypatch.setattr(
        portfolio_manager.regime_engine,
        "_get_required_history_dates",
        lambda _required_points: required_dates,
    )
    return required_dates


def _mock_required_regime_history_dates(portfolio_manager, mocker) -> None:
    mocker.patch.object(
        portfolio_manager.regime_engine,
        "_get_required_history_dates",
        side_effect=_required_regime_history_dates,
    )


def _seed_regime_history_cache(portfolio_manager, closes, symbols=REGIME_SYMBOLS):
    cache_bars = _regime_bars(closes)
    for symbol in symbols:
        portfolio_manager.data_store.record_historical_bars(
            symbol, REGIME_HISTORY_TIMEFRAME, cache_bars
        )


def _option_position(
    symbol: str,
    position: int,
    market_value: float,
    strike: float = 100.0,
    right: str = "C",
    expiry: str = "20270115",
    con_id: int = 0,
    average_cost: float | None = None,
    unrealized_pnl: float | None = None,
):
    contract = Option(
        symbol=symbol,
        lastTradeDateOrContractMonth=expiry,
        strike=strike,
        right=right,
        exchange="SMART",
        currency="USD",
    )
    contract.conId = con_id
    return SimpleNamespace(
        contract=contract,
        position=position,
        marketValue=market_value,
        averageCost=average_cost,
        unrealizedPNL=unrealized_pnl,
    )


def _tail_state(
    *,
    symbol: str = "BBB",
    puts: list[SimpleNamespace] | None = None,
    harvest_plans: list[dict] | None = None,
) -> dict:
    positions = puts or []
    tranches = []
    entry_history = []
    for index, position in enumerate(positions):
        entry_id = f"{symbol}-tail-{index}"
        entered_at = datetime(2026, 1, 1) + timedelta(days=index)
        quantity = int(position.position)
        tranches.append(
            {
                "entry_id": entry_id,
                "symbol": symbol,
                "status": "active",
                "con_id": position.contract.conId,
                "local_symbol": position.contract.localSymbol,
                "expiration": position.contract.lastTradeDateOrContractMonth,
                "strike": float(position.contract.strike),
                "quantity": quantity,
                "entry_limit_price": float(position.averageCost or 0.0) / 100.0,
                "entry_cost": float(position.averageCost or 0.0) * quantity,
                "entry_enqueued_at": entered_at,
            }
        )
        entry_history.append(
            {
                "entry_id": entry_id,
                "symbol": symbol,
                "entered_at": entered_at,
                "estimated_cost": float(position.averageCost or 0.0) * quantity,
            }
        )
    return {
        "schema_version": TAIL_HEDGE_STATE_SCHEMA_VERSION,
        "strategy": "long_put",
        "account": "TEST123",
        "status": "active",
        "tranches": tranches,
        "entry_history": entry_history,
        "harvest_plans": harvest_plans or [],
    }


def _tail_target(symbol: str = "BBB") -> SimpleNamespace:
    return SimpleNamespace(symbol=symbol)


def _option_ticker(price: float) -> SimpleNamespace:
    return SimpleNamespace(
        midpoint=lambda: price,
        marketPrice=lambda: price,
        modelGreeks=None,
    )


def _volatility_weight(
    *,
    target_vol: float = 0.32,
    lookback_days: int = 3,
    min_weight: float = 0.25,
    max_weight: float = 0.5,
    rebalance_band: float = 0.0,
    smoothing_factor: float = 1.0,
    increase_smoothing_factor: float | None = None,
    decrease_smoothing_factor: float | None = None,
):
    return SimpleNamespace(
        enabled=True,
        target_vol=target_vol,
        lookback_days=lookback_days,
        min_weight=min_weight,
        max_weight=max_weight,
        rebalance_band=rebalance_band,
        smoothing_factor=smoothing_factor,
        increase_smoothing_factor=increase_smoothing_factor,
        decrease_smoothing_factor=decrease_smoothing_factor,
    )


def _ratio_gate_result(
    portfolio_manager,
    *,
    closes_by_symbol,
    ratio_gate,
    effective_weights,
    lookback_days=3,
):
    start_date = datetime(2024, 1, 2)
    symbols = list(closes_by_symbol.keys())
    dates = [
        (start_date + timedelta(days=offset)).date()
        for offset in range(len(next(iter(closes_by_symbol.values()))))
    ]
    return portfolio_manager.regime_engine._calculate_ratio_gate(
        symbols=symbols,
        dates=dates,
        aligned_closes=closes_by_symbol,
        ratio_gate=ratio_gate,
        effective_weights=effective_weights,
        lookback_days=lookback_days,
        eps=1e-8,
    )


def _ratio_gate_config(
    *,
    enabled: bool = True,
    anchor: str = "BBB",
    drift_max: float = 1.25,
    vol_min: float = 0.0,
):
    return SimpleNamespace(
        enabled=enabled,
        anchor=anchor,
        drift_max=drift_max,
        vol_min=vol_min,
    )


def _configure_flow_rebalance(
    portfolio_manager,
    *,
    choppiness_min: float = 0.0,
    efficiency_max: float = 1.0,
    flow_trade_min: float = 0.10,
    flow_trade_stop: float = 0.05,
) -> None:
    regime_rebalance = portfolio_manager.config.strategies.regime_rebalance
    regime_rebalance.soft_band = 0.50
    regime_rebalance.hard_band = 0.80
    regime_rebalance.choppiness_min = choppiness_min
    regime_rebalance.efficiency_max = efficiency_max
    regime_rebalance.flow_trade_min = flow_trade_min
    regime_rebalance.flow_trade_stop = flow_trade_stop


@pytest.mark.asyncio
async def test_regime_rebalance_generates_orders(portfolio_manager, mocker):
    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}

    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -1), ("BBB", "NYSE", 1)]


@pytest.mark.asyncio
async def test_regime_rebalance_retries_empty_history(
    portfolio_manager, mocker, monkeypatch
):
    _disable_regime_history_retry_delay(monkeypatch)
    _mock_required_regime_history_dates(portfolio_manager, mocker)
    account_summary = _regime_account_summary()
    portfolio_positions = _regime_stock_positions()
    bars = _regime_bars([100.0, 110.0, 100.0, 110.0])
    attempts_by_symbol = {"AAA": 0, "BBB": 0}

    async def _get_history(contract, *_args, **_kwargs):
        attempts_by_symbol[contract.symbol] += 1
        if attempts_by_symbol[contract.symbol] == 1:
            return []
        return bars

    _mock_regime_broker(portfolio_manager, mocker, side_effect=_get_history)

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert attempts_by_symbol == {"AAA": 2, "BBB": 2}
    assert orders == [("AAA", "NYSE", -1), ("BBB", "NYSE", 1)]


@pytest.mark.asyncio
async def test_regime_rebalance_uses_fresh_cached_history_when_api_empty(
    portfolio_manager_with_db, mocker, monkeypatch
):
    _disable_regime_history_retry_delay(monkeypatch)
    _set_required_regime_history_dates(
        portfolio_manager_with_db, monkeypatch, required_points=4
    )
    _seed_regime_history_cache(portfolio_manager_with_db, [100.0, 110.0, 100.0, 110.0])
    account_summary = _regime_account_summary()
    portfolio_positions = _regime_stock_positions()

    _mock_regime_broker(portfolio_manager_with_db, mocker, return_value=[])

    _, orders = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert (
        portfolio_manager_with_db.ibkr.request_historical_data.call_count
        == _expected_regime_history_fetches()
    )
    assert orders == [("AAA", "NYSE", -1), ("BBB", "NYSE", 1)]


@pytest.mark.asyncio
async def test_regime_rebalance_uses_fresh_cache_when_api_history_is_stale(
    portfolio_manager_with_db, mocker, monkeypatch
):
    _disable_regime_history_retry_delay(monkeypatch)
    _set_required_regime_history_dates(
        portfolio_manager_with_db, monkeypatch, required_points=4
    )
    _seed_regime_history_cache(portfolio_manager_with_db, [100.0, 110.0, 100.0, 110.0])
    stale_bars = _regime_bars(
        [100.0, 99.0, 98.0, 97.0],
        start_date=datetime(2023, 12, 20),
    )
    account_summary = _regime_account_summary()
    portfolio_positions = _regime_stock_positions()

    _mock_regime_broker(portfolio_manager_with_db, mocker, return_value=stale_bars)

    _, orders = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert portfolio_manager_with_db.ibkr.request_historical_data.call_count == len(
        REGIME_SYMBOLS
    )
    assert orders == [("AAA", "NYSE", -1), ("BBB", "NYSE", 1)]


@pytest.mark.asyncio
async def test_regime_rebalance_ignores_history_after_required_sessions(
    portfolio_manager, mocker, monkeypatch
):
    _disable_regime_history_retry_delay(monkeypatch)
    required_dates = _set_required_regime_history_dates(
        portfolio_manager, monkeypatch, required_points=4
    )
    bars_with_extra_partial_session = _regime_bars([100.0, 110.0, 100.0, 110.0, 1.0])

    _mock_regime_broker(
        portfolio_manager, mocker, return_value=bars_with_extra_partial_session
    )

    (
        dates,
        aligned_closes,
    ) = await portfolio_manager.regime_engine._get_regime_aligned_closes(
        list(REGIME_SYMBOLS),
        lookback_days=3,
        cooldown_days=0,
    )

    assert dates == required_dates
    assert aligned_closes == {
        "AAA": [100.0, 110.0, 100.0, 110.0],
        "BBB": [100.0, 110.0, 100.0, 110.0],
    }
    assert portfolio_manager.ibkr.request_historical_data.call_count == len(
        REGIME_SYMBOLS
    )


@pytest.mark.asyncio
async def test_regime_rebalance_rejects_incomplete_cached_history(
    portfolio_manager_with_db, mocker, monkeypatch
):
    _disable_regime_history_retry_delay(monkeypatch)
    _set_required_regime_history_dates(
        portfolio_manager_with_db, monkeypatch, required_points=4
    )
    _seed_regime_history_cache(portfolio_manager_with_db, [100.0, 110.0, 100.0])
    account_summary = _regime_account_summary()
    portfolio_positions = _regime_stock_positions()

    _mock_regime_broker(portfolio_manager_with_db, mocker, return_value=[])

    with pytest.raises(ValueError, match="fresh historical data"):
        await portfolio_manager_with_db.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )

    assert (
        portfolio_manager_with_db.ibkr.request_historical_data.call_count
        == _expected_regime_history_fetches()
    )


@pytest.mark.asyncio
async def test_regime_rebalance_rejects_unreadable_cached_history(
    portfolio_manager_with_db, mocker, monkeypatch
):
    _disable_regime_history_retry_delay(monkeypatch)
    _set_required_regime_history_dates(
        portfolio_manager_with_db, monkeypatch, required_points=4
    )
    account_summary = _regime_account_summary()
    portfolio_positions = _regime_stock_positions()

    _mock_regime_broker(portfolio_manager_with_db, mocker, return_value=[])
    portfolio_manager_with_db.data_store.get_historical_bars = mocker.Mock(
        side_effect=RuntimeError("database unavailable")
    )

    with pytest.raises(ValueError, match="readable history cache"):
        await portfolio_manager_with_db.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )

    assert (
        portfolio_manager_with_db.ibkr.request_historical_data.call_count
        == _expected_regime_history_fetches()
    )
    assert portfolio_manager_with_db.data_store.get_historical_bars.call_count == 1


@pytest.mark.asyncio
async def test_regime_rebalance_volatility_weight_scales_down_without_renormalizing(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.10, min_weight=0.25, max_weight=0.5
    )
    portfolio_manager_with_db.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.efficiency_max = 1.0

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 2)],
        "BBB": [_stock_position("BBB", 2)],
    }

    _mock_regime_tickers(portfolio_manager_with_db, mocker)
    _mock_regime_history(
        portfolio_manager_with_db, mocker, [100.0, 200.0, 100.0, 200.0]
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    _, orders = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -1)]
    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "volatility_weight_state"
    )
    assert payload["total_effective_weight"] == pytest.approx(0.75)
    assert payload["unallocated_target_weight"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_regime_rebalance_volatility_weight_restores_only_to_base_weight(
    portfolio_manager, mocker
):
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.32, min_weight=0.25, max_weight=0.5
    )
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 2)],
        "BBB": [_stock_position("BBB", 2)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 101.0, 102.0, 103.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []


@pytest.mark.asyncio
async def test_regime_rebalance_volatility_weight_can_scale_above_base(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.portfolio.symbols["BBB"].weight = 0.4
    portfolio_manager_with_db.config.portfolio.symbols["SHV"] = SimpleNamespace(
        weight=0.1,
        primary_exchange="NYSE",
    )
    portfolio_manager_with_db.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.32,
        min_weight=0.25,
        max_weight=0.6,
        smoothing_factor=1.0,
    )
    portfolio_manager_with_db.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.efficiency_max = 1.0

    account_summary = {"NetLiquidation": SimpleNamespace(value="500")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 3)],
        "BBB": [_stock_position("BBB", 2)],
    }

    _mock_regime_tickers(portfolio_manager_with_db, mocker)
    _mock_regime_history(
        portfolio_manager_with_db, mocker, [100.0, 101.0, 102.0, 103.0]
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    _, orders = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "volatility_weight_state"
    )
    assert orders == []
    assert payload["symbols"]["AAA"]["effective_weight"] == pytest.approx(0.6)
    assert payload["total_effective_weight"] == pytest.approx(1.0)
    assert payload["unallocated_target_weight"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_regime_rebalance_rejects_effective_weights_above_100(
    portfolio_manager, mocker
):
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.32,
        min_weight=0.25,
        max_weight=0.6,
        smoothing_factor=1.0,
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="1000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 5)],
        "BBB": [_stock_position("BBB", 5)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 101.0, 102.0, 103.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="must not exceed 100%"):
        await portfolio_manager.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )


@pytest.mark.asyncio
async def test_regime_rebalance_managed_stocks_rejects_unallocated_weight(
    portfolio_manager, mocker
):
    portfolio_manager.config.strategies.regime_rebalance.weight_base = (
        RegimeRebalanceBaseEnum.managed_stocks
    )
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.10,
        min_weight=0.25,
        max_weight=0.5,
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 2)],
        "BBB": [_stock_position("BBB", 2)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 200.0, 100.0, 200.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="weight_base is managed_stocks"):
        await portfolio_manager.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )


@pytest.mark.asyncio
async def test_regime_rebalance_volatility_weight_smooths_from_previous_state(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.10,
        min_weight=0.25,
        max_weight=0.6,
        smoothing_factor=0.5,
    )
    portfolio_manager_with_db.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager_with_db.data_store.record_event(
        "volatility_weight_state",
        {"symbols": {"AAA": {"effective_weight": 0.5}}},
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 2)],
        "BBB": [_stock_position("BBB", 2)],
    }

    _mock_regime_tickers(portfolio_manager_with_db, mocker)
    _mock_regime_history(
        portfolio_manager_with_db, mocker, [100.0, 200.0, 100.0, 200.0]
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "volatility_weight_state"
    )
    assert payload["symbols"]["AAA"]["target_weight"] == pytest.approx(0.25)
    assert payload["symbols"]["AAA"]["effective_weight"] == pytest.approx(0.375)


@pytest.mark.asyncio
async def test_regime_rebalance_volatility_weight_batches_shared_lookbacks(
    portfolio_manager, mocker
):
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.32, min_weight=0.25, max_weight=0.6
    )
    portfolio_manager.config.portfolio.symbols[
        "BBB"
    ].volatility_weight = _volatility_weight(
        target_vol=0.32, min_weight=0.25, max_weight=0.6
    )
    _mock_regime_history(portfolio_manager, mocker, [100.0, 101.0, 102.0, 103.0])
    aligned_spy = mocker.spy(
        portfolio_manager.regime_engine,
        "_get_regime_aligned_closes",
    )

    await portfolio_manager.regime_engine._resolve_effective_weights(
        ["AAA", "BBB"],
        portfolio_manager.config.portfolio.symbols,
    )

    aligned_spy.assert_called_once()
    assert aligned_spy.call_args.args[:3] == (["AAA", "BBB"], 3, 0)


@pytest.mark.asyncio
async def test_regime_rebalance_volatility_weight_uses_increase_smoothing_factor(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.32,
        min_weight=0.25,
        max_weight=0.6,
        smoothing_factor=0.3,
        increase_smoothing_factor=0.2,
        decrease_smoothing_factor=0.5,
    )
    portfolio_manager_with_db.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager_with_db.data_store.record_event(
        "volatility_weight_state",
        {"symbols": {"AAA": {"effective_weight": 0.4}}},
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 2)],
        "BBB": [_stock_position("BBB", 2)],
    }

    _mock_regime_tickers(portfolio_manager_with_db, mocker)
    _mock_regime_history(
        portfolio_manager_with_db, mocker, [100.0, 101.0, 102.0, 103.0]
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "volatility_weight_state"
    )
    assert payload["symbols"]["AAA"]["target_weight"] == pytest.approx(0.6)
    assert payload["symbols"]["AAA"]["effective_weight"] == pytest.approx(0.44)
    assert payload["symbols"]["AAA"]["smoothing_factor"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_regime_rebalance_empty_proxy_fallback_uses_effective_weights(
    portfolio_manager, mocker
):
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.10,
        min_weight=0.25,
        max_weight=0.6,
        smoothing_factor=1.0,
    )
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0
    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {"AAA": [], "BBB": []}

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 200.0, 100.0, 200.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    proxy_spy = mocker.spy(portfolio_manager.regime_engine, "_get_regime_proxy_series")

    await portfolio_manager.check_regime_rebalance_positions(
        account_summary,
        portfolio_positions,
    )

    weights_override = proxy_spy.call_args.kwargs["weights_override"]
    assert weights_override["AAA"] == pytest.approx(0.25)
    assert weights_override["BBB"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_regime_rebalance_volatility_weight_band_does_not_block_smoothing(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.12,
        min_weight=0.25,
        max_weight=0.6,
        rebalance_band=0.03,
        smoothing_factor=0.3,
        increase_smoothing_factor=0.2,
        decrease_smoothing_factor=0.5,
    )
    portfolio_manager_with_db.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager_with_db.data_store.record_event(
        "volatility_weight_state",
        {"symbols": {"AAA": {"effective_weight": 0.4}}},
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 2)],
        "BBB": [_stock_position("BBB", 2)],
    }

    _mock_regime_tickers(portfolio_manager_with_db, mocker)
    _mock_regime_history(
        portfolio_manager_with_db, mocker, [100.0, 200.0, 100.0, 200.0]
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "volatility_weight_state"
    )
    assert payload["symbols"]["AAA"]["target_weight"] == pytest.approx(0.25)
    assert payload["symbols"]["AAA"]["effective_weight"] == pytest.approx(0.325)


@pytest.mark.asyncio
async def test_regime_rebalance_volatility_weight_without_db_starts_from_base(
    portfolio_manager, mocker
):
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.10,
        min_weight=0.25,
        max_weight=0.6,
        smoothing_factor=0.5,
    )
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 2)],
        "BBB": [_stock_position("BBB", 2)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 200.0, 100.0, 200.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    _, details = await portfolio_manager.regime_engine._resolve_effective_weights(
        ["AAA"],
        portfolio_manager.config.portfolio.symbols,
    )
    assert details["AAA"]["previous_weight"] == pytest.approx(0.5)
    assert details["AAA"]["effective_weight"] == pytest.approx(0.375)


@pytest.mark.asyncio
async def test_regime_rebalance_volatility_weight_falls_back_on_zero_vol(
    portfolio_manager, mocker
):
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.32, min_weight=0.25, max_weight=0.5
    )
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 2)],
        "BBB": [_stock_position("BBB", 2)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 100.0, 100.0, 100.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []


@pytest.mark.asyncio
async def test_regime_rebalance_volatility_weight_falls_back_on_invalid_closes(
    portfolio_manager, mocker
):
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.32, min_weight=0.25, max_weight=0.5
    )
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0

    _mock_regime_history(portfolio_manager, mocker, [100.0, 0.0, 100.0, 110.0])

    (
        effective_weights,
        details,
    ) = await portfolio_manager.regime_engine._resolve_effective_weights(
        ["AAA"],
        portfolio_manager.config.portfolio.symbols,
    )

    assert effective_weights["AAA"] == pytest.approx(0.5)
    assert details == {}


@pytest.mark.asyncio
async def test_regime_rebalance_volatility_weight_falls_back_on_short_history(
    portfolio_manager, mocker
):
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.32,
        lookback_days=10,
        min_weight=0.25,
        max_weight=0.5,
    )
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0

    _mock_regime_history(portfolio_manager, mocker, [100.0, 101.0, 102.0])

    (
        effective_weights,
        details,
    ) = await portfolio_manager.regime_engine._resolve_effective_weights(
        ["AAA"],
        portfolio_manager.config.portfolio.symbols,
    )

    assert effective_weights["AAA"] == pytest.approx(0.5)
    assert details == {}


@pytest.mark.asyncio
async def test_regime_rebalance_volatility_weight_state_payload_fields(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.10,
        min_weight=0.25,
        max_weight=0.6,
        smoothing_factor=0.5,
    )
    portfolio_manager_with_db.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager_with_db.data_store.record_event(
        "volatility_weight_state",
        {"symbols": {"AAA": {"effective_weight": 0.5}}},
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 2)],
        "BBB": [_stock_position("BBB", 2)],
    }

    _mock_regime_tickers(portfolio_manager_with_db, mocker)
    _mock_regime_history(
        portfolio_manager_with_db, mocker, [100.0, 200.0, 100.0, 200.0]
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "volatility_weight_state"
    )
    assert set(payload["symbols"]["AAA"].keys()) == {
        "base_weight",
        "effective_weight",
        "target_weight",
        "realized_vol",
        "smoothing_factor",
    }


@pytest.mark.asyncio
async def test_regime_rebalance_excludes_options_and_cash_fund_from_base(
    portfolio_manager, mocker
):
    portfolio_manager.config.runtime.account.margin_usage = 1.2
    portfolio_manager.config.strategies.regime_rebalance.weight_base = (
        RegimeRebalanceBaseEnum.net_liq_ex_options
    )
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0

    account_summary = {"NetLiquidation": SimpleNamespace(value="100000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 500)],
        "BBB": [_stock_position("BBB", 300)],
        "SHV": [_stock_position("SHV", 100, market_value=15000.0)],
        "AAA_OPT": [_option_position("AAA", 1, market_value=10000.0)],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", 40), ("BBB", "NYSE", 240)]


@pytest.mark.asyncio
@pytest.mark.parametrize("tail_enabled", [True, False])
async def test_regime_rebalance_excludes_state_owned_tail_puts_from_base(
    portfolio_manager_with_db, mocker, tail_enabled
):
    portfolio_manager_with_db.config.runtime.account.margin_usage = 1.2
    regime_config = portfolio_manager_with_db.config.strategies.regime_rebalance
    regime_config.weight_base = RegimeRebalanceBaseEnum.net_liq_ex_options
    regime_config.soft_band = 0.0
    regime_config.choppiness_min = 0.0
    regime_config.efficiency_max = 1.0
    portfolio_manager_with_db.config.strategies.tail_hedge = SimpleNamespace(
        enabled=tail_enabled,
        targets=[SimpleNamespace(symbol="AAA")],
    )
    tail_con_id = 701
    assert portfolio_manager_with_db.data_store.record_event(
        "tail_hedge_state",
        {
            "schema_version": TAIL_HEDGE_STATE_SCHEMA_VERSION,
            "strategy": "long_put",
            "account": "TEST123",
            "status": "active",
            "tranches": [
                {
                    "entry_id": "tail-entry",
                    "symbol": "AAA",
                    "con_id": tail_con_id,
                    "expiration": "20270115",
                }
            ],
            "entry_history": [],
            "harvest_plans": [],
        },
        symbol="AAA",
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="100000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 500)],
        "BBB": [_stock_position("BBB", 300)],
        "AAA_OPT": [
            _option_position(
                "AAA",
                1,
                market_value=10_000.0,
                right="P",
                con_id=tail_con_id,
                average_cost=10_001.0,
                unrealized_pnl=-1.0,
            ),
            _option_position(
                "AAA",
                1,
                market_value=5_000.0,
                right="P",
                con_id=702,
            ),
        ],
    }

    _mock_regime_tickers(
        portfolio_manager_with_db,
        mocker,
        aaa_price=100.0,
        bbb_price=100.0,
    )
    _mock_regime_history(
        portfolio_manager_with_db,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    _, orders = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary,
        portfolio_positions,
    )

    # Both the state-owned tail put and unrelated option are removed from core
    # capital: floor(($100k - $15k) * 1.2) = $102k, or $51k per stock.
    assert orders == [("AAA", "NYSE", 10), ("BBB", "NYSE", 210)]


@pytest.mark.asyncio
@pytest.mark.parametrize("tail_enabled", [True, False])
async def test_net_liq_base_also_excludes_state_owned_tail_puts(
    portfolio_manager_with_db, mocker, tail_enabled
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.runtime.account.margin_usage = 1.2
    regime_config = portfolio_manager.config.strategies.regime_rebalance
    regime_config.weight_base = RegimeRebalanceBaseEnum.net_liq
    regime_config.soft_band = 0.0
    regime_config.choppiness_min = 0.0
    regime_config.efficiency_max = 1.0
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=tail_enabled,
        targets=[_tail_target("AAA")],
    )
    tail_put = _option_position(
        "AAA",
        1,
        market_value=10_000.0,
        right="P",
        con_id=701,
        average_cost=10_001.0,
        unrealized_pnl=-1.0,
    )
    assert portfolio_manager.data_store.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _tail_state(symbol="AAA", puts=[tail_put]),
    )
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 500), tail_put],
        "BBB": [_stock_position("BBB", 300)],
        "OTHER_OPT": [_option_position("AAA", 1, market_value=5_000.0, con_id=702)],
    }
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        _regime_account_summary("100000"),
        portfolio_positions,
    )

    # Only the $10k state-owned tail put is removed in net_liq mode; the
    # unrelated $5k option remains in the selected base by configuration.
    assert orders == [("AAA", "NYSE", 40), ("BBB", "NYSE", 240)]


@pytest.mark.asyncio
async def test_regime_rebalance_box_spread_borrowing_excluded_from_base(
    portfolio_manager, mocker
):
    portfolio_manager.config.runtime.account.margin_usage = 1.2
    portfolio_manager.config.strategies.regime_rebalance.weight_base = (
        RegimeRebalanceBaseEnum.net_liq_ex_options
    )
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.0
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0

    account_summary = {"NetLiquidation": SimpleNamespace(value="100000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 400)],
        "BBB": [_stock_position("BBB", 400)],
        "SHV": [_stock_position("SHV", 200, market_value=20000.0)],
        "SPX": [
            _option_position(
                "SPX",
                -1,
                market_value=-12000.0,
                strike=5000.0,
                right="C",
                expiry="20260716",
            ),
            _option_position(
                "SPX",
                1,
                market_value=8000.0,
                strike=5000.0,
                right="P",
                expiry="20260716",
            ),
        ],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", 224), ("BBB", "NYSE", 224)]


@pytest.mark.asyncio
async def test_regime_rebalance_respects_regime_gate(portfolio_manager, mocker):
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 10.0

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}

    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []


@pytest.mark.asyncio
async def test_regime_rebalance_ratio_gate_shadow_metrics_emitted(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.strategies.regime_rebalance.ratio_gate = (
        _ratio_gate_config(enabled=False)
    )
    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager_with_db, mocker)
    _mock_regime_history(
        portfolio_manager_with_db, mocker, [100.0, 110.0, 100.0, 110.0]
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "regime_rebalance_gate"
    )
    assert payload["ratio_gate"]["enabled"] is False
    assert payload["ratio_gate"]["anchor"] == "BBB"
    assert payload["ratio_gate"]["rest"] == ["AAA"]


@pytest.mark.asyncio
async def test_regime_rebalance_ratio_gate_blocks_soft_rebalance(
    portfolio_manager, mocker
):
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.strategies.regime_rebalance.ratio_gate = (
        _ratio_gate_config()
    )
    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 100.0, 100.0, 100.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []


@pytest.mark.asyncio
async def test_regime_rebalance_hard_band_ignores_ratio_gate(portfolio_manager, mocker):
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.30
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.10
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 10.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 0.01
    portfolio_manager.config.strategies.regime_rebalance.ratio_gate = (
        _ratio_gate_config()
    )
    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 100.0, 100.0, 100.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -1), ("BBB", "NYSE", 1)]


@pytest.mark.asyncio
async def test_regime_rebalance_ratio_gate_handles_uninvested_rest_symbol(
    portfolio_manager, mocker
):
    portfolio_manager.config.portfolio.symbols["AAA"].weight = 0.4
    portfolio_manager.config.portfolio.symbols["BBB"].weight = 0.4
    portfolio_manager.config.portfolio.symbols["CCC"] = SimpleNamespace(
        weight=0.2, primary_exchange="NYSE"
    )
    portfolio_manager.config.strategies.regime_rebalance.symbols = [
        "AAA",
        "BBB",
        "CCC",
    ]
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.strategies.regime_rebalance.ratio_gate = (
        _ratio_gate_config()
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="500")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(
        portfolio_manager,
        mocker,
        extra_prices={"CCC": 100.0},
    )
    _mock_regime_history(portfolio_manager, mocker, [100.0, 100.0, 100.0, 100.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert isinstance(orders, list)


def test_regime_rebalance_ratio_gate_reports_nonzero_rebased_metrics(
    portfolio_manager,
):
    result = _ratio_gate_result(
        portfolio_manager,
        closes_by_symbol={
            "AAA": [100.0, 110.0, 105.0, 120.0],
            "BBB": [100.0, 101.0, 99.0, 103.0],
        },
        ratio_gate=_ratio_gate_config(drift_max=999.0),
        effective_weights={"AAA": 0.5, "BBB": 0.5},
    )

    assert result.daily_var is not None
    assert result.daily_std is not None
    assert result.annualized_vol is not None
    assert result.daily_var > 0.0
    assert result.daily_std > 0.0
    assert result.annualized_vol == pytest.approx(result.daily_std * math.sqrt(252))
    assert result.ok is True


def test_regime_rebalance_ratio_gate_vol_min_is_annualized(portfolio_manager):
    result = _ratio_gate_result(
        portfolio_manager,
        closes_by_symbol={
            "AAA": [100.0, 110.0, 105.0, 120.0],
            "BBB": [100.0, 101.0, 99.0, 103.0],
        },
        ratio_gate=_ratio_gate_config(drift_max=999.0, vol_min=0.05),
        effective_weights={"AAA": 0.5, "BBB": 0.5},
    )

    assert result.daily_var is not None
    assert result.annualized_vol is not None
    assert result.daily_var < 0.05
    assert result.annualized_vol >= 0.05
    assert result.vol_min == pytest.approx(0.05)
    assert result.ok is True


def test_regime_rebalance_ratio_gate_rebased_index_is_price_scale_invariant(
    portfolio_manager,
):
    closes_by_symbol = {
        "AAA": [50.0, 55.0, 52.0, 56.0],
        "BBB": [30.0, 31.0, 30.0, 32.0],
        "CCC": [20.0, 19.0, 21.0, 22.0],
    }
    scaled_closes_by_symbol = {
        **closes_by_symbol,
        "AAA": [price * 10 for price in closes_by_symbol["AAA"]],
    }
    ratio_gate = _ratio_gate_config(drift_max=999.0)
    effective_weights = {"AAA": 0.4, "BBB": 0.4, "CCC": 0.2}

    base_result = _ratio_gate_result(
        portfolio_manager,
        closes_by_symbol=closes_by_symbol,
        ratio_gate=ratio_gate,
        effective_weights=effective_weights,
    )
    scaled_result = _ratio_gate_result(
        portfolio_manager,
        closes_by_symbol=scaled_closes_by_symbol,
        ratio_gate=ratio_gate,
        effective_weights=effective_weights,
    )

    assert scaled_result.daily_mean == pytest.approx(base_result.daily_mean)
    assert scaled_result.daily_std == pytest.approx(base_result.daily_std)
    assert scaled_result.daily_var == pytest.approx(base_result.daily_var)
    assert scaled_result.annualized_vol == pytest.approx(base_result.annualized_vol)
    assert scaled_result.tstat == pytest.approx(base_result.tstat)


@pytest.mark.asyncio
async def test_regime_rebalance_ratio_gate_uses_effective_rest_weights(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.portfolio.symbols["AAA"].weight = 0.4
    portfolio_manager_with_db.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.05,
        min_weight=0.1,
        max_weight=0.4,
        smoothing_factor=1.0,
    )
    portfolio_manager_with_db.config.portfolio.symbols["BBB"].weight = 0.4
    portfolio_manager_with_db.config.portfolio.symbols["CCC"] = SimpleNamespace(
        weight=0.2,
        primary_exchange="NYSE",
    )
    portfolio_manager_with_db.config.strategies.regime_rebalance.symbols = [
        "AAA",
        "BBB",
        "CCC",
    ]
    portfolio_manager_with_db.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.ratio_gate = (
        _ratio_gate_config(enabled=False, drift_max=999.0)
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="1000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 1)],
        "BBB": [_stock_position("BBB", 1)],
        "CCC": [_stock_position("CCC", 1)],
    }

    _mock_regime_tickers(
        portfolio_manager_with_db,
        mocker,
        aaa_price=100.0,
        bbb_price=100.0,
        extra_prices={"CCC": 100.0},
    )
    _mock_regime_histories(
        portfolio_manager_with_db,
        mocker,
        {
            "AAA": [100.0, 200.0, 100.0, 200.0],
            "BBB": [100.0, 101.0, 100.0, 102.0],
            "CCC": [100.0, 102.0, 104.0, 106.0],
        },
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary,
        portfolio_positions,
    )

    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "regime_rebalance_gate"
    )
    ratio_payload = payload["ratio_gate"]
    assert ratio_payload["weights"]["AAA"] == pytest.approx(1 / 3)
    assert ratio_payload["weights"]["CCC"] == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_regime_rebalance_cooldown_blocks_trades(
    portfolio_manager, mocker, monkeypatch
):
    now = datetime(2024, 1, 5, 12, 0, 0)
    _freeze_now(monkeypatch, now)

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}

    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    bars = _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])

    last_fill_date = bars[-1].date
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(
        return_value=[
            SimpleNamespace(
                execution=SimpleNamespace(
                    orderRef="tg:regime-rebalance:AAA", time=last_fill_date
                ),
                contract=SimpleNamespace(symbol="AAA"),
                time=last_fill_date,
            )
        ]
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []


@pytest.mark.asyncio
async def test_regime_rebalance_cooldown_allows_after_window(
    portfolio_manager, mocker, monkeypatch
):
    now = datetime(2024, 1, 5, 12, 0, 0)
    _freeze_now(monkeypatch, now)

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}

    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    bars = _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])

    last_fill_date = bars[0].date
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(
        return_value=[
            SimpleNamespace(
                execution=SimpleNamespace(
                    orderRef="tg:regime-rebalance:AAA", time=last_fill_date
                ),
                contract=SimpleNamespace(symbol="AAA"),
                time=last_fill_date,
            )
        ]
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -1), ("BBB", "NYSE", 1)]


@pytest.mark.asyncio
async def test_regime_rebalance_cooldown_blocks_same_day_missing_bar(
    portfolio_manager, mocker, monkeypatch
):
    now = datetime(2024, 1, 10, 12, 0, 0)
    _freeze_now(monkeypatch, now)

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}

    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])

    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(
        return_value=[
            SimpleNamespace(
                execution=SimpleNamespace(orderRef="tg:regime-rebalance:AAA", time=now),
                contract=SimpleNamespace(symbol="AAA"),
                time=now,
            )
        ]
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []


@pytest.mark.asyncio
async def test_regime_rebalance_ignores_non_matching_order_refs(
    portfolio_manager, mocker
):
    fills = [
        SimpleNamespace(
            execution=SimpleNamespace(
                orderRef="tg:other:AAA", time=datetime(2024, 1, 5)
            ),
            contract=SimpleNamespace(symbol="AAA"),
            time=datetime(2024, 1, 5),
        ),
        SimpleNamespace(
            execution=SimpleNamespace(
                orderRef="tg:regime-rebalance:CCC", time=datetime(2024, 1, 6)
            ),
            contract=SimpleNamespace(symbol="CCC"),
            time=datetime(2024, 1, 6),
        ),
        SimpleNamespace(
            execution=SimpleNamespace(
                orderRef="tg:regime-rebalance:BBB", time=datetime(2024, 1, 7)
            ),
            contract=SimpleNamespace(symbol="BBB"),
            time=datetime(2024, 1, 7),
        ),
    ]
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=fills)

    last_rebalance = await portfolio_manager._get_last_regime_rebalance_time(
        ["AAA", "BBB"]
    )

    assert last_rebalance == datetime(2024, 1, 7)


@pytest.mark.asyncio
async def test_regime_rebalance_uses_db_for_cooldown(
    portfolio_manager_with_db, mocker, monkeypatch
):
    fills = [
        SimpleNamespace(
            execution=SimpleNamespace(
                execId="1",
                orderRef="tg:regime-rebalance:AAA",
                time=datetime(2024, 1, 5, 12, 0, 0),
            ),
            contract=SimpleNamespace(symbol="AAA"),
            time=datetime(2024, 1, 5, 12, 0, 0),
        ),
        SimpleNamespace(
            execution=SimpleNamespace(
                execId="2",
                orderRef="tg:regime-rebalance:BBB",
                time=datetime(2024, 1, 7, 12, 0, 0),
            ),
            contract=SimpleNamespace(symbol="BBB"),
            time=datetime(2024, 1, 7, 12, 0, 0),
        ),
    ]
    portfolio_manager_with_db.data_store.record_executions(fills)
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )
    _freeze_now(monkeypatch, datetime(2024, 1, 10, 12, 0, 0))

    last_rebalance = await portfolio_manager_with_db._get_last_regime_rebalance_time(
        ["AAA", "BBB"]
    )

    assert last_rebalance == datetime(2024, 1, 7, 12, 0, 0)


@pytest.mark.asyncio
async def test_regime_rebalance_insufficient_history(portfolio_manager, mocker):
    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}

    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="fresh historical data"):
        await portfolio_manager.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )


@pytest.mark.asyncio
async def test_regime_rebalance_band_thresholds(portfolio_manager, mocker):
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.10
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.50
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0

    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=50.0)
    below_band_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=10)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=19)],
    }
    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, below_band_positions
    )
    assert orders == []

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=50.0)
    at_band_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=12)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=16)],
    }
    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, at_band_positions
    )
    assert orders == [("AAA", "NYSE", -2), ("BBB", "NYSE", 4)]


@pytest.mark.asyncio
async def test_regime_rebalance_hard_band_ignores_regime_and_cooldown(
    portfolio_manager, mocker, monkeypatch
):
    now = datetime(2024, 1, 5, 12, 0, 0)
    _freeze_now(monkeypatch, now)

    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.30
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.10
    portfolio_manager.config.strategies.regime_rebalance.cooldown_days = 10
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 10.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 0.01

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    bars = _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    last_fill_date = bars[-1].date
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(
        return_value=[
            SimpleNamespace(
                execution=SimpleNamespace(
                    orderRef="tg:regime-rebalance:AAA", time=last_fill_date
                ),
                contract=SimpleNamespace(symbol="AAA"),
                time=last_fill_date,
            )
        ]
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -1), ("BBB", "NYSE", 1)]


@pytest.mark.asyncio
async def test_regime_rebalance_hard_band_partial_rebalance(portfolio_manager, mocker):
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.30
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.10
    portfolio_manager.config.strategies.regime_rebalance.hard_band_rebalance_fraction = 0.5
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 10.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 0.01

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=20)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=0)],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -5), ("BBB", "NYSE", 5)]


@pytest.mark.asyncio
async def test_regime_rebalance_soft_band_blocked_by_regime(portfolio_manager, mocker):
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.10
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.80
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 10.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 0.01

    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []


@pytest.mark.asyncio
async def test_regime_rebalance_positive_flow_bypasses_regime_gate(
    portfolio_manager_with_db, mocker
):
    _configure_flow_rebalance(
        portfolio_manager_with_db,
        choppiness_min=10.0,
        efficiency_max=0.01,
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 8)],
        "BBB": [_stock_position("BBB", 8)],
    }

    _mock_regime_tickers(
        portfolio_manager_with_db,
        mocker,
        aaa_price=100.0,
        bbb_price=100.0,
    )
    _mock_regime_history(
        portfolio_manager_with_db,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )
    info_mock = mocker.patch.object(regime_engine_module.log, "info")

    _, orders = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", 2), ("BBB", "NYSE", 2)]
    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "regime_rebalance_gate"
    )
    assert payload["telemetry_schema_version"] == 3
    assert payload["legacy_field_aliases"] == {
        "excess_cash": "unallocated_rebalance_capacity"
    }
    assert payload["unallocated_rebalance_capacity"] == pytest.approx(400.0)
    assert payload["excess_cash"] == pytest.approx(400.0)
    assert payload["choppiness_ok"] is False
    assert payload["regime_ok"] is False
    assert payload["shared_rebalance_gates_ok"] is False
    assert payload["mode"] == "flow"
    assert payload["flow"] == {
        "signal_kind": "inferred_unallocated_rebalance_capacity",
        "classification": "inferred_capacity_deployment",
        "external_flow_detection": "not_performed",
        "capacity_source": "rebalance_base_minus_managed_sleeve_value",
        "weight_base": "net_liq_ex_options",
        "margin_usage": 1.0,
        "rebalance_base": 2000,
        "managed_sleeve_value": 1600.0,
        "unallocated_rebalance_capacity": 400.0,
        "direction": "buy",
        "gate": True,
        "decision_status": "selected",
        "shared_rebalance_gates_ok": False,
        "shared_gate_blockers": ["regime"],
        "eligibility_gates_ok": True,
        "eligibility_gate_blockers": [],
        "rebalance_eligible": True,
        "selected": True,
        "was_active": False,
        "will_be_active": True,
        "start_threshold": 200.0,
        "stop_threshold": 100.0,
        "candidate_symbols": ["AAA", "BBB"],
        "net_share_gap": 4,
        "total_absolute_share_gap": 4,
        "net_value_gap": 400.0,
        "total_absolute_value_gap": 400.0,
        "imbalance_unit": "dollars",
        "imbalance_ratio": 1.0,
        "imbalance_tau": 0.7,
        "directional_imbalance_ok": True,
        "orders": [["AAA", "NYSE", 2], ["BBB", "NYSE", 2]],
        "reserved_cash_for_post_management": 0.0,
    }
    assert payload["deficit"] == {
        "gate": False,
        "was_active": False,
        "will_be_active": False,
        "start_threshold": 120.0,
        "stop_threshold": 60.0,
    }
    summary_payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "regime_rebalance_summary"
    )
    assert summary_payload["telemetry_schema_version"] == 3
    assert summary_payload["unallocated_rebalance_capacity"] == pytest.approx(400.0)
    assert summary_payload["flow"] == payload["flow"]
    assert summary_payload["deficit"] == payload["deficit"]
    info_messages = [call.args[0] for call in info_mock.call_args_list]
    flow_message = next(
        message
        for message in info_messages
        if message.startswith("Regime rebalancing inferred-capacity flow:")
    )
    assert "classification=inferred_capacity_deployment" in flow_message
    assert "decision=selected" in flow_message
    assert "ordinary_gate_failures=regime" in flow_message
    assert "eligibility_gate_blockers=none" in flow_message
    assert "eligibility_gates_ok=True" in flow_message
    assert "was_active=False will_be_active=True" in flow_message


@pytest.mark.asyncio
async def test_regime_rebalance_positive_flow_fills_unequal_price_target_gaps(
    portfolio_manager, mocker
):
    _configure_flow_rebalance(
        portfolio_manager,
        choppiness_min=10.0,
        efficiency_max=0.01,
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 8)],
        "BBB": [_stock_position("BBB", 16)],
    }

    _mock_regime_tickers(
        portfolio_manager,
        mocker,
        aaa_price=100.0,
        bbb_price=50.0,
    )
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", 2), ("BBB", "NYSE", 4)]


@pytest.mark.asyncio
async def test_regime_rebalance_positive_flow_scales_to_available_capacity(
    portfolio_manager, mocker
):
    _configure_flow_rebalance(portfolio_manager)
    regime_rebalance = portfolio_manager.config.strategies.regime_rebalance
    regime_rebalance.soft_band = 1.2
    regime_rebalance.hard_band = 1.5
    regime_rebalance.flow_imbalance_tau = 0.6

    account_summary = {"NetLiquidation": SimpleNamespace(value="1000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 0)],
        "BBB": [_stock_position("BBB", 12)],
    }

    _mock_regime_tickers(
        portfolio_manager,
        mocker,
        aaa_price=100.0,
        bbb_price=50.0,
    )
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", 4)]


@pytest.mark.asyncio
async def test_regime_rebalance_directional_gate_uses_dollar_gaps(
    portfolio_manager_with_db, mocker
):
    _configure_flow_rebalance(
        portfolio_manager_with_db,
        flow_trade_min=0.005,
        flow_trade_stop=0.0,
    )
    regime_rebalance = portfolio_manager_with_db.config.strategies.regime_rebalance
    regime_rebalance.soft_band = 2.0
    regime_rebalance.hard_band = 3.0

    account_summary = {"NetLiquidation": SimpleNamespace(value="10000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 55)],
        "BBB": [_stock_position("BBB", 440)],
    }

    _mock_regime_tickers(
        portfolio_manager_with_db,
        mocker,
        aaa_price=100.0,
        bbb_price=10.0,
    )
    _mock_regime_history(
        portfolio_manager_with_db, mocker, [100.0, 110.0, 100.0, 110.0]
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    _, orders = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []
    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "regime_rebalance_gate"
    )
    assert payload["flow"]["net_share_gap"] == 55
    assert payload["flow"]["net_value_gap"] == pytest.approx(100.0)
    assert payload["flow"]["total_absolute_value_gap"] == pytest.approx(1100.0)
    assert payload["flow"]["imbalance_unit"] == "dollars"
    assert payload["flow"]["imbalance_ratio"] == pytest.approx(1 / 11)
    assert payload["flow"]["decision_status"] == "blocked_by_directional_imbalance"


@pytest.mark.asyncio
async def test_regime_rebalance_negative_flow_sells_by_excess_value(
    portfolio_manager, mocker
):
    _configure_flow_rebalance(
        portfolio_manager,
        flow_trade_min=0.05,
        flow_trade_stop=0.025,
    )
    regime_rebalance = portfolio_manager.config.strategies.regime_rebalance
    regime_rebalance.soft_band = 2.0
    regime_rebalance.hard_band = 3.0
    regime_rebalance.deficit_rail_start = 0.2
    regime_rebalance.deficit_rail_stop = 0.1

    account_summary = {"NetLiquidation": SimpleNamespace(value="10000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 55)],
        "BBB": [_stock_position("BBB", 540)],
    }

    _mock_regime_tickers(
        portfolio_manager,
        mocker,
        aaa_price=100.0,
        bbb_price=10.0,
    )
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -5), ("BBB", "NYSE", -40)]


@pytest.mark.asyncio
async def test_regime_rebalance_positive_flow_uses_volatility_adjusted_target(
    portfolio_manager, mocker
):
    _configure_flow_rebalance(
        portfolio_manager,
        choppiness_min=10.0,
        efficiency_max=0.01,
    )
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.90
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.95
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.10,
        min_weight=0.25,
        max_weight=0.5,
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 2)],
        "BBB": [_stock_position("BBB", 8)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 200.0, 100.0, 200.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", 3), ("BBB", "NYSE", 2)]
    assert portfolio_manager.get_reserved_cash_for_post_management() == 0.0


@pytest.mark.asyncio
async def test_regime_rebalance_cooldown_blocks_positive_flow(
    portfolio_manager, mocker, monkeypatch
):
    _configure_flow_rebalance(portfolio_manager)
    now = datetime(2024, 1, 5, 12, 0, 0)
    _freeze_now(monkeypatch, now)

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 8)],
        "BBB": [_stock_position("BBB", 8)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    bars = _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    last_fill_date = bars[-1].date
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(
        return_value=[
            SimpleNamespace(
                execution=SimpleNamespace(
                    orderRef="tg:regime-rebalance:AAA", time=last_fill_date
                ),
                contract=SimpleNamespace(symbol="AAA"),
                time=last_fill_date,
            )
        ]
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []


@pytest.mark.asyncio
async def test_regime_rebalance_directional_gate_blocks_positive_flow(
    portfolio_manager_with_db, mocker
):
    _configure_flow_rebalance(portfolio_manager_with_db)
    portfolio_manager_with_db.config.strategies.regime_rebalance.soft_band = 0.60
    portfolio_manager_with_db.config.strategies.regime_rebalance.hard_band = 0.90

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 5)],
        "BBB": [_stock_position("BBB", 12)],
    }

    _mock_regime_tickers(portfolio_manager_with_db, mocker)
    _mock_regime_history(
        portfolio_manager_with_db, mocker, [100.0, 110.0, 100.0, 110.0]
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    _, orders = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []
    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "regime_rebalance_gate"
    )
    assert payload["flow"]["decision_status"] == "blocked_by_directional_imbalance"
    assert payload["flow"]["eligibility_gates_ok"] is False
    assert payload["flow"]["eligibility_gate_blockers"] == ["directional_imbalance"]
    assert payload["flow"]["rebalance_eligible"] is False


@pytest.mark.asyncio
async def test_regime_rebalance_negative_flow_retains_regime_gate(
    portfolio_manager_with_db, mocker
):
    _configure_flow_rebalance(
        portfolio_manager_with_db,
        choppiness_min=10.0,
        efficiency_max=0.01,
    )
    regime_rebalance = portfolio_manager_with_db.config.strategies.regime_rebalance
    regime_rebalance.deficit_rail_start = 0.50
    regime_rebalance.deficit_rail_stop = 0.25

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 12)],
        "BBB": [_stock_position("BBB", 12)],
    }

    _mock_regime_tickers(portfolio_manager_with_db, mocker)
    _mock_regime_history(
        portfolio_manager_with_db, mocker, [100.0, 110.0, 100.0, 110.0]
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    _, orders = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []
    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "regime_rebalance_gate"
    )
    assert payload["flow"]["classification"] == "inferred_capacity_reduction"
    assert payload["flow"]["decision_status"] == "blocked_by_shared_gates"
    assert payload["flow"]["eligibility_gate_blockers"] == ["regime"]


@pytest.mark.asyncio
async def test_regime_rebalance_ratio_gate_blocks_flow_rebalance(
    portfolio_manager, mocker
):
    _configure_flow_rebalance(portfolio_manager)
    portfolio_manager.config.strategies.regime_rebalance.ratio_gate = (
        _ratio_gate_config(vol_min=0.05)
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 8)],
        "BBB": [_stock_position("BBB", 8)],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []


@pytest.mark.asyncio
async def test_regime_rebalance_blocked_flow_preserves_hysteresis(
    portfolio_manager_with_db, mocker
):
    _configure_flow_rebalance(
        portfolio_manager_with_db,
        flow_trade_min=0.25,
        flow_trade_stop=0.05,
    )
    portfolio_manager_with_db.config.strategies.regime_rebalance.ratio_gate = (
        _ratio_gate_config(vol_min=0.05)
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}

    _mock_regime_tickers(
        portfolio_manager_with_db,
        mocker,
        aaa_price=100.0,
        bbb_price=100.0,
    )
    _mock_regime_history(
        portfolio_manager_with_db,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    (
        _,
        blocked_orders,
    ) = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary,
        {
            "AAA": [_stock_position("AAA", 7)],
            "BBB": [_stock_position("BBB", 7)],
        },
    )

    assert blocked_orders == []
    assert portfolio_manager_with_db.data_store.get_last_event_payload(
        "regime_rebalance_state"
    ) == {"flow_active": True, "deficit_active": False}
    blocked_payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "regime_rebalance_gate"
    )
    assert blocked_payload["flow"]["decision_status"] == "blocked_by_shared_gates"
    assert blocked_payload["flow"]["shared_gate_blockers"] == ["ratio"]
    assert blocked_payload["flow"]["eligibility_gates_ok"] is False
    assert blocked_payload["flow"]["eligibility_gate_blockers"] == ["ratio"]
    assert blocked_payload["flow"]["was_active"] is False
    assert blocked_payload["flow"]["will_be_active"] is True

    portfolio_manager_with_db.config.strategies.regime_rebalance.ratio_gate = None

    (
        _,
        resumed_orders,
    ) = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary,
        {
            "AAA": [_stock_position("AAA", 8)],
            "BBB": [_stock_position("BBB", 8)],
        },
    )

    assert resumed_orders == [("AAA", "NYSE", 2), ("BBB", "NYSE", 2)]


def test_normalize_config_converts_parts_to_weights():
    config = {
        "account": {},
        "ibc": {},
        "target": {},
        "roll_when": {},
        "symbols": {
            "AAA": {"parts": 30},
            "BBB": {"parts": 30},
            "CCC": {"parts": 40},
        },
    }

    normalized = normalize_config(config)

    assert "parts" not in normalized["symbols"]["AAA"]
    assert normalized["symbols"]["AAA"]["weight"] == pytest.approx(0.3)
    assert normalized["symbols"]["BBB"]["weight"] == pytest.approx(0.3)
    assert normalized["symbols"]["CCC"]["weight"] == pytest.approx(0.4)


def test_regime_rebalance_shares_only_disables_options(portfolio_manager):
    portfolio_manager.config.strategies.regime_rebalance.shares_only = True
    assert portfolio_manager.options_trading_enabled() is False


def test_regime_rebalance_config_rejects_inverted_bands():
    with pytest.raises(ValueError, match="hard_band"):
        RegimeRebalanceConfig(soft_band=0.5, hard_band=0.25)


def test_regime_rebalance_config_rejects_flow_hysteresis_inversion():
    with pytest.raises(ValueError, match="flow_trade_min"):
        RegimeRebalanceConfig(flow_trade_min=0.10, flow_trade_stop=0.20)


def test_regime_rebalance_config_rejects_deficit_hysteresis_inversion():
    with pytest.raises(ValueError, match="deficit_rail_start"):
        RegimeRebalanceConfig(deficit_rail_start=0.10, deficit_rail_stop=0.20)


def test_regime_rebalance_config_accepts_ratio_gate_vol_min():
    config = RegimeRebalanceConfig(
        symbols=["AAA", "BBB"],
        ratio_gate=RatioGateConfig(
            enabled=True,
            anchor="AAA",
            vol_min=0.05,
        ),
    )

    assert config.ratio_gate is not None
    assert config.ratio_gate.vol_min == pytest.approx(0.05)


def test_regime_rebalance_config_rejects_ratio_gate_var_min():
    with pytest.raises(
        ValueError,
        match=(
            "ratio_gate.var_min has been removed; use vol_min instead.*"
            r"vol_min = sqrt\(var_min \* 252\)"
        ),
    ):
        RatioGateConfig.model_validate(
            {
                "enabled": True,
                "anchor": "AAA",
                "var_min": 0.01,
            }
        )


def test_regime_rebalance_config_rejects_ratio_gate_missing_anchor():
    with pytest.raises(ValueError, match="ratio_gate.anchor must be set"):
        RegimeRebalanceConfig(
            symbols=["AAA", "BBB"],
            ratio_gate=RatioGateConfig(enabled=True, anchor=""),
        )


def test_regime_rebalance_config_rejects_ratio_gate_anchor_not_in_symbols():
    with pytest.raises(ValueError, match="ratio_gate.anchor must be in"):
        RegimeRebalanceConfig(
            symbols=["AAA", "BBB"],
            ratio_gate=RatioGateConfig(enabled=True, anchor="CCC"),
        )


def test_regime_rebalance_config_rejects_ratio_gate_only_anchor_symbol():
    with pytest.raises(ValueError, match="ratio_gate.anchor must leave"):
        RegimeRebalanceConfig(
            symbols=["AAA"],
            ratio_gate=RatioGateConfig(enabled=True, anchor="AAA"),
        )


@pytest.mark.asyncio
async def test_regime_rebalance_respects_no_trading(portfolio_manager, mocker):
    portfolio_manager.config.trading_is_allowed = mocker.Mock(
        side_effect=lambda symbol: symbol != "AAA"
    )
    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}

    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []


@pytest.mark.asyncio
async def test_regime_rebalance_cash_added_triggers_buys(portfolio_manager, mocker):
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.5
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.8
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.strategies.regime_rebalance.flow_trade_min = 0.10
    portfolio_manager.config.strategies.regime_rebalance.flow_trade_stop = 0.05

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=8)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=8)],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", 2), ("BBB", "NYSE", 2)]
    assert portfolio_manager.get_reserved_cash_for_post_management() == 0.0


@pytest.mark.asyncio
async def test_regime_rebalance_cash_flow_does_not_reserve_disabled_target_gaps(
    portfolio_manager, mocker
):
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.90
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.95
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.strategies.regime_rebalance.flow_trade_min = 0.10
    portfolio_manager.config.strategies.regime_rebalance.flow_trade_stop = 0.05
    portfolio_manager.config.trading_is_allowed = mocker.Mock(
        side_effect=lambda symbol: symbol == "AAA"
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="10000")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=8)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=8)],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", 42)]
    assert portfolio_manager.get_reserved_cash_for_post_management() == 0.0


@pytest.mark.asyncio
async def test_regime_rebalance_cash_flow_reserves_actionable_rounding_remainder(
    portfolio_manager, mocker
):
    _configure_flow_rebalance(portfolio_manager)
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.90
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.95

    account_summary = {"NetLiquidation": SimpleNamespace(value="1000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 2)],
        "BBB": [_stock_position("BBB", 4)],
    }

    _mock_regime_tickers(
        portfolio_manager,
        mocker,
        aaa_price=120.0,
        bbb_price=150.0,
    )
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", 1)]
    assert portfolio_manager.get_reserved_cash_for_post_management() == 40.0


@pytest.mark.asyncio
async def test_regime_rebalance_flow_gate_without_buys_does_not_reserve_cash(
    portfolio_manager, mocker
):
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.90
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.95
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.strategies.regime_rebalance.flow_trade_min = 0.10
    portfolio_manager.config.strategies.regime_rebalance.flow_trade_stop = 0.05
    portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=False)

    account_summary = {"NetLiquidation": SimpleNamespace(value="10000")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=8)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=8)],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []
    assert portfolio_manager.get_reserved_cash_for_post_management() == 0.0


@pytest.mark.asyncio
async def test_regime_rebalance_cash_withdrawn_triggers_sells(
    portfolio_manager, mocker
):
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.5
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.8
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.strategies.regime_rebalance.flow_trade_min = 0.10
    portfolio_manager.config.strategies.regime_rebalance.flow_trade_stop = 0.05

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=12)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=12)],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -2), ("BBB", "NYSE", -2)]
    assert portfolio_manager.get_reserved_cash_for_post_management() == 0.0


@pytest.mark.asyncio
async def test_regime_rebalance_flow_hysteresis_uses_db_state(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.strategies.regime_rebalance.soft_band = 0.5
    portfolio_manager_with_db.config.strategies.regime_rebalance.hard_band = 0.8
    portfolio_manager_with_db.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager_with_db.config.strategies.regime_rebalance.flow_trade_min = 0.25
    portfolio_manager_with_db.config.strategies.regime_rebalance.flow_trade_stop = 0.05

    portfolio_manager_with_db.data_store.record_event(
        "regime_rebalance_state", {"flow_active": True, "deficit_active": False}
    )

    account_summary = {"NetLiquidation": SimpleNamespace(value="2000")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=8)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=8)],
    }

    _mock_regime_tickers(
        portfolio_manager_with_db, mocker, aaa_price=100.0, bbb_price=100.0
    )
    _mock_regime_history(
        portfolio_manager_with_db, mocker, [100.0, 110.0, 100.0, 110.0]
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    _, orders = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", 2), ("BBB", "NYSE", 2)]


@pytest.mark.asyncio
async def test_regime_rebalance_deficit_rail_sells_overweights(
    portfolio_manager_with_db, mocker
):
    regime_rebalance = portfolio_manager_with_db.config.strategies.regime_rebalance
    regime_rebalance.soft_band = 1.2
    regime_rebalance.hard_band = 1.5
    regime_rebalance.choppiness_min = 0.0
    regime_rebalance.efficiency_max = 1.0
    regime_rebalance.deficit_rail_start = 0.30
    regime_rebalance.deficit_rail_stop = 0.10

    account_summary = {"NetLiquidation": SimpleNamespace(value="1000")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=10)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=5)],
    }

    _mock_regime_tickers(
        portfolio_manager_with_db,
        mocker,
        aaa_price=100.0,
        bbb_price=100.0,
    )
    _mock_regime_history(
        portfolio_manager_with_db,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    _, orders = await portfolio_manager_with_db.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -4)]
    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "regime_rebalance_gate"
    )
    assert payload["mode"] == "deficit"
    assert payload["flow"]["gate"] is False
    assert payload["flow"]["decision_status"] == "superseded_by_deficit_rail"
    assert payload["deficit"]["gate"] is True


@pytest.mark.asyncio
async def test_regime_rebalance_deficit_rail_sells_pro_rata(portfolio_manager, mocker):
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.3
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.8
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.strategies.regime_rebalance.deficit_rail_start = 0.10
    portfolio_manager.config.strategies.regime_rebalance.deficit_rail_stop = 0.05

    account_summary = {"NetLiquidation": SimpleNamespace(value="1000")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=6)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=6)],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -1), ("BBB", "NYSE", -1)]


@pytest.mark.asyncio
async def test_regime_rebalance_deficit_rail_sells_by_overweight_value(
    portfolio_manager, mocker
):
    regime_rebalance = portfolio_manager.config.strategies.regime_rebalance
    regime_rebalance.soft_band = 2.0
    regime_rebalance.hard_band = 3.0
    regime_rebalance.choppiness_min = 0.0
    regime_rebalance.efficiency_max = 1.0
    regime_rebalance.deficit_rail_start = 0.10
    regime_rebalance.deficit_rail_stop = 0.03

    account_summary = {"NetLiquidation": SimpleNamespace(value="10000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 61)],
        "BBB": [_stock_position("BBB", 601)],
    }

    _mock_regime_tickers(
        portfolio_manager,
        mocker,
        aaa_price=100.0,
        bbb_price=10.0,
    )
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -10), ("BBB", "NYSE", -81)]


@pytest.mark.asyncio
async def test_regime_rebalance_deficit_rail_sells_from_initial_amount(
    portfolio_manager, mocker
):
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 1.2
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 1.5
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.strategies.regime_rebalance.deficit_rail_start = 0.10
    portfolio_manager.config.strategies.regime_rebalance.deficit_rail_stop = 0.0

    account_summary = {"NetLiquidation": SimpleNamespace(value="1000")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=10)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=10)],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -5), ("BBB", "NYSE", -5)]


@pytest.mark.asyncio
async def test_regime_rebalance_deficit_cleanup_uses_stop_band(
    portfolio_manager, mocker
):
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.10
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.20
    portfolio_manager.config.strategies.regime_rebalance.hard_band_rebalance_fraction = 0.5
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.strategies.regime_rebalance.deficit_rail_start = 0.50
    portfolio_manager.config.strategies.regime_rebalance.deficit_rail_stop = 0.20

    account_summary = {"NetLiquidation": SimpleNamespace(value="1000")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=10)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=5)],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", -3)]


@pytest.mark.asyncio
async def test_regime_rebalance_no_trading_blocks_deficit_and_hard(
    portfolio_manager, mocker
):
    portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=False)
    portfolio_manager.config.strategies.regime_rebalance.soft_band = 0.10
    portfolio_manager.config.strategies.regime_rebalance.hard_band = 0.20
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.strategies.regime_rebalance.deficit_rail_start = 0.10
    portfolio_manager.config.strategies.regime_rebalance.deficit_rail_stop = 0.05

    account_summary = {"NetLiquidation": SimpleNamespace(value="1000")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=10)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=5)],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []


@pytest.mark.asyncio
async def test_regime_rebalance_invalid_market_price_raises(portfolio_manager, mocker):
    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=0.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="valid market prices"):
        await portfolio_manager.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )


@pytest.mark.asyncio
async def test_regime_rebalance_invalid_close_raises(portfolio_manager, mocker):
    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 0.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="invalid historical closes"):
        await portfolio_manager.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )


@pytest.mark.asyncio
async def test_regime_rebalance_no_common_dates_raises(portfolio_manager, mocker):
    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    start_date = datetime(2024, 1, 2)
    aaa_bars = [
        SimpleNamespace(date=start_date + timedelta(days=offset), close=100.0)
        for offset in range(3)
    ]
    bbb_bars = [
        SimpleNamespace(date=start_date + timedelta(days=offset + 10), close=100.0)
        for offset in range(3)
    ]

    async def _get_history(contract, *_args, **_kwargs):
        if contract.symbol == "AAA":
            return aaa_bars
        return bbb_bars

    portfolio_manager.ibkr.request_historical_data = mocker.AsyncMock(
        side_effect=_get_history
    )

    with pytest.raises(ValueError, match="aligned history"):
        await portfolio_manager.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )


@pytest.mark.asyncio
async def test_regime_rebalance_empty_history_stays_hard_failure(
    portfolio_manager, mocker, monkeypatch
):
    _disable_regime_history_retry_delay(monkeypatch)
    account_summary = _regime_account_summary()
    portfolio_positions = _regime_stock_positions()

    _mock_regime_broker(portfolio_manager, mocker, return_value=[])

    with pytest.raises(ValueError, match="aligned history"):
        await portfolio_manager.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )

    assert (
        portfolio_manager.ibkr.request_historical_data.call_count
        == _expected_regime_history_fetches()
    )


@pytest.mark.asyncio
async def test_regime_rebalance_zero_weights_raises(portfolio_manager, mocker):
    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }
    portfolio_manager.config.portfolio.symbols["AAA"].weight = 0.0
    portfolio_manager.config.portfolio.symbols["BBB"].weight = 0.0

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="positive target weights"):
        await portfolio_manager.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )


@pytest.mark.asyncio
async def test_actionable_buy_harvests_shortest_profitable_puts_and_defers_stock(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=True,
        targets=[_tail_target("BBB")],
    )
    short_put = _option_position(
        "BBB",
        1,
        market_value=60.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
        unrealized_pnl=10.0,
    )
    short_put.contract.multiplier = "100"
    later_put = _option_position(
        "BBB",
        2,
        market_value=120.0,
        right="P",
        expiry="20261218",
        con_id=802,
        average_cost=50.0,
        unrealized_pnl=20.0,
    )
    later_put.contract.multiplier = "100"
    assert portfolio_manager.data_store.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _tail_state(symbol="BBB", puts=[later_put, short_put]),
    )

    account_summary = _regime_account_summary("580")
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 3)],
        "BBB": [_stock_position("BBB", 0), short_put, later_put],
        "OTHER": [_stock_position("OTHER", 1, market_value=100.0)],
    }
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_option_ticker(0.60)
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        account_summary,
        portfolio_positions,
    )

    assert orders == [("AAA", "NYSE", -1)]
    queued = portfolio_manager.orders.records()
    assert [contract.conId for contract, _order, _intent_id in queued] == [801, 802]
    assert [int(order.totalQuantity) for _contract, order, _intent_id in queued] == [
        1,
        1,
    ]
    assert all(
        order.orderRef.startswith(TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX)
        for _contract, order, _intent_id in queued
    )
    assert all(len(order.orderRef) <= 32 for _contract, order, _intent_id in queued)
    state = portfolio_manager.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
    plan = state["harvest_plans"][0]
    assert plan["status"] == "put_sell_working"
    assert plan["approved_buy_amount"] == 100.0
    assert plan["target_snapshot"]["ordinary_approved_shares"] == 2
    assert plan["target_snapshot"]["funding"]["funding_shortfall"] == 100.0
    assert [sale["con_id"] for sale in plan["put_sales"]] == [801, 802]
    assert sum(sale["estimated_proceeds"] for sale in plan["put_sales"]) == 120.0
    assert portfolio_manager.get_reserved_cash_for_post_management() == 120.0


@pytest.mark.asyncio
async def test_cash_deposit_funds_hard_rebalance_without_harvesting_puts(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=True,
        targets=[_tail_target("BBB")],
    )
    tail_put = _option_position(
        "BBB",
        1,
        market_value=60.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
        unrealized_pnl=10.0,
    )
    tail_put.contract.multiplier = "100"
    assert portfolio_manager.data_store.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _tail_state(symbol="BBB", puts=[tail_put]),
    )
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        _regime_account_summary("560", cash="100"),
        {
            "AAA": [_stock_position("AAA", 3)],
            "BBB": [_stock_position("BBB", 0), tail_put],
            "OTHER": [_stock_position("OTHER", 1, market_value=100.0)],
        },
    )

    assert orders == [("AAA", "NYSE", -1), ("BBB", "NYSE", 2)]
    assert portfolio_manager.orders.records() == []
    state = portfolio_manager.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
    assert state["harvest_plans"] == []


@pytest.mark.asyncio
async def test_queued_buy_debit_is_reserved_before_tail_harvesting(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=True,
        targets=[_tail_target("BBB")],
    )
    tail_put = _option_position(
        "BBB",
        1,
        market_value=60.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
        unrealized_pnl=10.0,
    )
    tail_put.contract.multiplier = "100"
    assert portfolio_manager.data_store.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _tail_state(symbol="BBB", puts=[tail_put]),
    )
    portfolio_manager.orders.add_order(
        Stock("OTHER", "SMART", "USD"),
        SimpleNamespace(action="BUY", lmtPrice=100.0, totalQuantity=1),
        None,
    )
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_option_ticker(0.60)
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        _regime_account_summary("560", cash="100"),
        {
            "AAA": [_stock_position("AAA", 3)],
            "BBB": [_stock_position("BBB", 0), tail_put],
            "OTHER": [_stock_position("OTHER", 1, market_value=100.0)],
        },
    )

    assert orders == [("AAA", "NYSE", -1)]
    state = portfolio_manager.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
    plan = state["harvest_plans"][0]
    funding = plan["target_snapshot"]["funding"]
    assert funding["queued_cash_debits"] == 100.0
    assert funding["funding_shortfall"] == 100.0


@pytest.mark.asyncio
async def test_cash_fund_liquidity_is_used_before_tail_harvesting(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.strategies.cash_management = SimpleNamespace(
        enabled=True,
        cash_fund="SHV",
        target_cash_balance=50,
    )
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=True,
        targets=[_tail_target("BBB")],
    )
    tail_put = _option_position(
        "BBB",
        1,
        market_value=60.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
        unrealized_pnl=10.0,
    )
    tail_put.contract.multiplier = "100"
    assert portfolio_manager.data_store.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _tail_state(symbol="BBB", puts=[tail_put]),
    )
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        _regime_account_summary("610", cash="0"),
        {
            "AAA": [_stock_position("AAA", 3)],
            "BBB": [_stock_position("BBB", 0), tail_put],
            "OTHER": [_stock_position("OTHER", 1, market_value=100.0)],
            "SHV": [_stock_position("SHV", 1, market_value=150.0)],
        },
    )

    assert orders == [("AAA", "NYSE", -1), ("BBB", "NYSE", 2)]
    assert portfolio_manager.orders.records() == []
    state = portfolio_manager.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
    assert state["harvest_plans"] == []


@pytest.mark.asyncio
async def test_other_symbol_hard_breach_does_not_harvest_protected_symbol(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=True,
        targets=[_tail_target("BBB")],
    )
    tail_put = _option_position(
        "BBB",
        1,
        market_value=60.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
        unrealized_pnl=10.0,
    )
    tail_put.contract.multiplier = "100"
    assert portfolio_manager.data_store.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _tail_state(symbol="BBB", puts=[tail_put]),
    )
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        _regime_account_summary("1060"),
        {
            "AAA": [_stock_position("AAA", 1)],
            "BBB": [_stock_position("BBB", 4), tail_put],
            "OTHER": [_stock_position("OTHER", 1, market_value=500.0)],
        },
    )

    assert orders == [("AAA", "NYSE", 4), ("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []
    state = portfolio_manager.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
    assert state["harvest_plans"] == []


@pytest.mark.asyncio
async def test_released_tail_credit_funds_other_orders_before_new_harvest(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=True,
        targets=[_tail_target("AAA"), _tail_target("BBB")],
    )
    bbb_put = _option_position(
        "BBB",
        1,
        market_value=60.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
        unrealized_pnl=10.0,
    )
    bbb_put.contract.multiplier = "100"
    ready_plan = {
        "plan_id": "aaa-ready",
        "symbol": "AAA",
        "status": "rebalance_credit_ready",
        "requested_at": datetime(2026, 8, 12, 12, 0, 0),
        "approved_buy_amount": 200.0,
        "target_snapshot": {"approved_shares": 2},
        "put_sales": [],
        "actual_put_sale_proceeds": 200.0,
        "remaining_rebalance_credit": 200.0,
    }
    assert portfolio_manager.data_store.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _tail_state(symbol="BBB", puts=[bbb_put], harvest_plans=[ready_plan]),
    )
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        _regime_account_summary("660", cash="200"),
        {
            "AAA": [_stock_position("AAA", 2)],
            "BBB": [_stock_position("BBB", 0), bbb_put],
            "OTHER": [_stock_position("OTHER", 2, market_value=200.0)],
        },
    )

    assert orders == [("BBB", "NYSE", 2)]
    assert portfolio_manager.orders.records() == []
    state = portfolio_manager.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
    assert len(state["harvest_plans"]) == 1
    assert state["harvest_plans"][0]["status"] == "completed"
    assert state["harvest_plans"][0]["unused_proceeds"] == 200.0


@pytest.mark.asyncio
async def test_funding_shortfall_is_allocated_proportionally_across_tail_targets(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=True,
        targets=[_tail_target("AAA"), _tail_target("BBB")],
    )
    aaa_put = _option_position(
        "AAA",
        3,
        market_value=180.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
        unrealized_pnl=30.0,
    )
    bbb_put = _option_position(
        "BBB",
        3,
        market_value=180.0,
        right="P",
        expiry="20261218",
        con_id=802,
        average_cost=50.0,
        unrealized_pnl=30.0,
    )
    aaa_put.contract.multiplier = "100"
    bbb_put.contract.multiplier = "100"
    aaa_state = _tail_state(symbol="AAA", puts=[aaa_put])
    bbb_state = _tail_state(symbol="BBB", puts=[bbb_put])
    state = {
        **aaa_state,
        "tranches": aaa_state["tranches"] + bbb_state["tranches"],
        "entry_history": (aaa_state["entry_history"] + bbb_state["entry_history"]),
    }
    assert portfolio_manager.data_store.record_event(TAIL_HEDGE_STATE_EVENT, state)
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_option_ticker(1.0)
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        _regime_account_summary("1360", cash="200"),
        {
            "AAA": [_stock_position("AAA", 1), aaa_put],
            "BBB": [_stock_position("BBB", 1), bbb_put],
            "OTHER": [_stock_position("OTHER", 1, market_value=600.0)],
        },
    )

    assert orders == []
    queued = portfolio_manager.orders.records()
    assert [contract.conId for contract, _order, _intent_id in queued] == [801, 802]
    assert [int(order.totalQuantity) for _contract, order, _intent_id in queued] == [
        3,
        3,
    ]
    state = portfolio_manager.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
    assert [plan["approved_buy_amount"] for plan in state["harvest_plans"]] == [
        300.0,
        300.0,
    ]
    assert all(
        plan["target_snapshot"]["funding"]["funding_shortfall"] == 600.0
        for plan in state["harvest_plans"]
    )


@pytest.mark.asyncio
async def test_realized_tail_credit_is_capped_by_original_approval_on_later_run(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=True,
        targets=[_tail_target("BBB")],
    )
    plan_id = "credit-cap"
    harvest_order_ref = f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:{plan_id}:801"
    harvest_plan = {
        "plan_id": plan_id,
        "symbol": "BBB",
        "status": "put_sell_working",
        "requested_at": datetime(2026, 8, 12, 12, 0, 0),
        "approved_buy_amount": 100.0,
        "target_snapshot": {"approved_shares": 1},
        "put_sales": [
            {
                "entry_id": "bbb-tail",
                "con_id": 801,
                "expiration": "20261120",
                "quantity": 1,
                "multiplier": 100.0,
                "estimated_proceeds": 300.0,
                "order_ref": harvest_order_ref,
                "order_status": "enqueued",
            }
        ],
        "actual_put_sale_proceeds": 0.0,
        "remaining_rebalance_credit": 0.0,
    }
    assert portfolio_manager.data_store.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _tail_state(harvest_plans=[harvest_plan]),
    )
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            orderRef=harvest_order_ref,
            side="SLD",
            shares=1,
            price=3.0,
        ),
        commissionReport=SimpleNamespace(commission=1.25),
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[fill])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        _regime_account_summary("698.75"),
        {
            "AAA": [_stock_position("AAA", 4)],
            "BBB": [_stock_position("BBB", 0)],
        },
    )

    assert ("BBB", "NYSE", 1) in orders
    assert ("BBB", "NYSE", 2) not in orders
    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_option_ticker(100.0)
    )
    await portfolio_manager.execute_regime_rebalance_orders(
        [order for order in orders if order[0] == "BBB"]
    )

    stock_orders = portfolio_manager.orders.records()
    assert len(stock_orders) == 1
    contract, order, _intent_id = stock_orders[0]
    assert isinstance(contract, Stock)
    assert int(order.totalQuantity) == 1
    assert order.orderRef == "tg:regime-rebalance:credit-cap"
    assert len(order.orderRef) <= 32
    state = portfolio_manager.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
    plan = state["harvest_plans"][0]
    assert plan["status"] == "stock_buy_enqueued"
    assert plan["actual_put_sale_proceeds"] == 298.75
    assert plan["stock_buy"]["authorized_amount"] == 100.0


@pytest.mark.asyncio
async def test_filled_harvest_leaves_credit_as_cash_when_buy_gap_disappears(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=True,
        targets=[_tail_target("BBB")],
    )
    plan_id = "gap-gone"
    harvest_order_ref = f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:{plan_id}:801"
    harvest_plan = {
        "plan_id": plan_id,
        "symbol": "BBB",
        "status": "put_sell_working",
        "requested_at": datetime(2026, 8, 12, 12, 0, 0),
        "approved_buy_amount": 100.0,
        "target_snapshot": {"approved_shares": 1},
        "put_sales": [
            {
                "entry_id": "bbb-tail",
                "con_id": 801,
                "expiration": "20261120",
                "quantity": 1,
                "multiplier": 100.0,
                "estimated_proceeds": 150.0,
                "order_ref": harvest_order_ref,
                "order_status": "enqueued",
            }
        ],
        "actual_put_sale_proceeds": 0.0,
        "remaining_rebalance_credit": 0.0,
    }
    assert portfolio_manager.data_store.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _tail_state(harvest_plans=[harvest_plan]),
    )
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            orderRef=harvest_order_ref,
            side="SLD",
            shares=1,
            price=1.5,
        )
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[fill])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        _regime_account_summary("550"),
        {
            "AAA": [_stock_position("AAA", 2)],
            "BBB": [_stock_position("BBB", 2)],
        },
    )

    assert orders == []
    state = portfolio_manager.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
    plan = state["harvest_plans"][0]
    assert plan["status"] == "completed"
    assert plan["completion_reason"] == "current_buy_not_actionable"
    assert plan["actual_put_sale_proceeds"] == 150.0
    assert plan["unused_proceeds"] == 150.0
    assert portfolio_manager.get_reserved_cash_for_post_management() == 0.0


@pytest.mark.asyncio
async def test_working_harvest_reconciles_when_tail_entries_are_disabled(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=False,
        targets=[],
    )
    plan_id = "disabled-after-sale"
    harvest_order_ref = f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:{plan_id}:801"
    harvest_plan = {
        "plan_id": plan_id,
        "symbol": "BBB",
        "status": "put_sell_working",
        "requested_at": datetime(2026, 8, 12, 12, 0, 0),
        "approved_buy_amount": 100.0,
        "target_snapshot": {"approved_shares": 1},
        "put_sales": [
            {
                "entry_id": "bbb-tail",
                "con_id": 801,
                "expiration": "20261120",
                "quantity": 1,
                "multiplier": 100.0,
                "estimated_proceeds": 150.0,
                "order_ref": harvest_order_ref,
                "order_status": "enqueued",
            }
        ],
        "actual_put_sale_proceeds": 0.0,
        "remaining_rebalance_credit": 0.0,
    }
    assert portfolio_manager.data_store.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _tail_state(harvest_plans=[harvest_plan]),
    )
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            orderRef=harvest_order_ref,
            side="SLD",
            shares=1,
            price=1.5,
        )
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[fill])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        _regime_account_summary("550"),
        {
            "AAA": [_stock_position("AAA", 4)],
            "BBB": [_stock_position("BBB", 0)],
        },
    )

    assert ("BBB", "NYSE", 2) in orders
    state = portfolio_manager.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
    plan = state["harvest_plans"][0]
    assert plan["status"] == "completed"
    assert plan["completion_reason"] == "tail_target_removed"
    assert plan["actual_put_sale_proceeds"] == 150.0
    assert plan["unused_proceeds"] == 150.0
    assert portfolio_manager.get_reserved_cash_for_post_management() == 0.0


@pytest.mark.asyncio
async def test_tail_funded_stock_fill_completes_plan_with_unused_cash(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=True,
        targets=[_tail_target("BBB")],
    )
    plan_id = "stock-filled"
    stock_order_ref = f"tg:regime-rebalance:{plan_id}"
    harvest_plan = {
        "plan_id": plan_id,
        "symbol": "BBB",
        "status": "stock_buy_enqueued",
        "requested_at": datetime(2026, 8, 12, 12, 0, 0),
        "approved_buy_amount": 100.0,
        "target_snapshot": {"approved_shares": 1},
        "put_sales": [],
        "actual_put_sale_proceeds": 120.0,
        "remaining_rebalance_credit": 120.0,
        "stock_buy": {
            "order_ref": stock_order_ref,
            "order_status": "enqueued",
            "quantity": 1,
            "limit_price": 100.0,
            "authorized_amount": 100.0,
        },
    }
    assert portfolio_manager.data_store.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _tail_state(harvest_plans=[harvest_plan]),
    )
    fill_time = datetime.now() - timedelta(days=1)
    fill = SimpleNamespace(
        time=fill_time,
        contract=Stock("BBB", "SMART", "USD"),
        execution=SimpleNamespace(
            orderRef=stock_order_ref,
            side="BOT",
            shares=1,
            price=99.0,
            time=fill_time,
        ),
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[fill])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        _regime_account_summary("400"),
        {
            "AAA": [_stock_position("AAA", 2)],
            "BBB": [_stock_position("BBB", 2)],
        },
    )

    assert orders == []
    state = portfolio_manager.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
    plan = state["harvest_plans"][0]
    assert plan["status"] == "completed"
    assert plan["stock_buy"]["filled_quantity"] == 1
    assert plan["stock_buy"]["actual_cost"] == 99.0
    assert plan["unused_proceeds"] == 21.0
    assert portfolio_manager.get_reserved_cash_for_post_management() == 0.0


@pytest.mark.asyncio
async def test_tail_credit_does_not_bypass_current_minimum_trade_threshold(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=True,
        targets=[_tail_target("BBB")],
    )
    portfolio_manager.config.regime_rebalance_policy = lambda _symbol: SimpleNamespace(
        allows_buy=lambda: True,
        allows_sell=lambda: True,
        mode=SimpleNamespace(value="both"),
        min_threshold_shares=2,
        min_threshold_amount=None,
        min_threshold_percent=None,
        min_threshold_percent_relative=None,
    )
    plan_id = "minimum-threshold"
    harvest_order_ref = f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:{plan_id}:801"
    harvest_plan = {
        "plan_id": plan_id,
        "symbol": "BBB",
        "status": "put_sell_working",
        "requested_at": datetime(2026, 8, 12, 12, 0, 0),
        "approved_buy_amount": 200.0,
        "target_snapshot": {"approved_shares": 2},
        "put_sales": [
            {
                "entry_id": "bbb-tail",
                "con_id": 801,
                "expiration": "20261120",
                "quantity": 1,
                "multiplier": 100.0,
                "estimated_proceeds": 150.0,
                "order_ref": harvest_order_ref,
                "order_status": "enqueued",
            }
        ],
        "actual_put_sale_proceeds": 0.0,
        "remaining_rebalance_credit": 0.0,
    }
    assert portfolio_manager.data_store.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _tail_state(harvest_plans=[harvest_plan]),
    )
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            orderRef=harvest_order_ref,
            side="SLD",
            shares=1,
            price=1.5,
        )
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[fill])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )

    _, orders = await portfolio_manager.check_regime_rebalance_positions(
        _regime_account_summary("550"),
        {
            "AAA": [_stock_position("AAA", 4)],
            "BBB": [_stock_position("BBB", 0)],
        },
    )

    assert orders == [("AAA", "NYSE", -2)]
    state = portfolio_manager.data_store.get_last_event_payload(TAIL_HEDGE_STATE_EVENT)
    plan = state["harvest_plans"][0]
    assert plan["status"] == "completed"
    assert plan["completion_reason"] == "funded_buy_below_current_minimum_threshold"
    assert plan["unused_proceeds"] == 150.0
