import math
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from ib_async import IB, AccountValue, MarketOrder, Option, Stock

import thetagang.portfolio_manager as pm_module
import thetagang.strategies.regime_engine as regime_engine_module
from thetagang.config import Config
from thetagang.db import DataStore, ExecutionRecord
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
    TAIL_HEDGE_HARVEST_EVENT,
)
from thetagang.strategies.tail_hedge_state import (
    TAIL_HEDGE_CLOSE_ORDER_REF,
    TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX,
    TAIL_HEDGE_MIN_LIMIT_PRICE_ATTR,
    TailHedgeCohort,
    TailHedgeState,
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


def _enable_tail_hedge_stage(portfolio_manager) -> None:
    portfolio_manager.run_stage_flags["equity_regime_rebalance"] = True
    portfolio_manager.run_stage_flags["post_tail_hedge"] = True


def _enable_cash_management_stage(portfolio_manager, enabled: bool = True) -> None:
    portfolio_manager.run_stage_flags["post_cash_management"] = enabled


def _stock_position(
    symbol: str,
    position: int | float,
    market_value: float | None = None,
):
    return SimpleNamespace(
        account="TEST123",
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
        account="TEST123",
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
) -> TailHedgeState:
    positions = puts or []
    cohorts = []
    for index, position in enumerate(positions):
        entry_id = f"{symbol}-tail-{position.contract.conId}"
        entered_at = datetime(2026, 1, 1) + timedelta(days=index)
        quantity = int(position.position)
        average_cost = float(position.averageCost or 0.0)
        if not math.isfinite(average_cost) or average_cost <= 0:
            average_cost = 50.0
        cohorts.append(
            TailHedgeCohort(
                entry_id=entry_id,
                symbol=symbol,
                status="active",
                con_id=position.contract.conId,
                expiration=position.contract.lastTradeDateOrContractMonth,
                strike=float(position.contract.strike),
                quantity=quantity,
                entry_limit_price=average_cost / 100.0,
                entered_at=entered_at,
                estimated_cost=average_cost * quantity,
            )
        )
    return TailHedgeState(cohorts=cohorts)


def _save_tail_state(portfolio_manager, *states: TailHedgeState) -> None:
    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None
    store.save(
        TailHedgeState(
            cohorts=[cohort for state in states for cohort in state.cohorts],
        )
    )


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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    (
        _,
        orders,
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    (
        _,
        orders,
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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
        await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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
        await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    (
        _,
        orders,
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    (
        _,
        orders,
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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
        await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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
        await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "volatility_weight_state"
    )
    assert payload["symbols"]["AAA"]["target_weight"] == pytest.approx(0.25)
    assert payload["symbols"]["AAA"]["effective_weight"] == pytest.approx(0.375)

    await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
        account_summary,
        portfolio_positions,
        exclude_current_run_state=True,
    )

    payload = portfolio_manager_with_db.data_store.get_last_event_payload(
        "volatility_weight_state"
    )
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

    await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", 40), ("BBB", "NYSE", 240)]


@pytest.mark.asyncio
async def test_net_liq_base_also_excludes_state_owned_tail_puts(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.runtime.account.margin_usage = 1.2
    regime_config = portfolio_manager.config.strategies.regime_rebalance
    regime_config.weight_base = RegimeRebalanceBaseEnum.net_liq
    regime_config.soft_band = 0.0
    regime_config.choppiness_min = 0.0
    regime_config.efficiency_max = 1.0
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=False,
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
    _save_tail_state(
        portfolio_manager,
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
        _regime_account_summary("100000"),
        portfolio_positions,
    )

    # Only the $10k state-owned tail put is removed in net_liq mode; the
    # unrelated $5k option remains in the selected base by configuration.
    assert orders == [("AAA", "NYSE", 40), ("BBB", "NYSE", 240)]


@pytest.mark.asyncio
async def test_net_liq_ex_options_preserves_tail_ownership_outside_regime(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.runtime.account.margin_usage = 1.2
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=False,
        targets=[_tail_target("CCC")],
    )
    tail_put = _option_position(
        "CCC",
        1,
        market_value=10_000.0,
        right="P",
        con_id=703,
        average_cost=10_001.0,
        unrealized_pnl=-1.0,
    )
    _save_tail_state(
        portfolio_manager,
        _tail_state(symbol="CCC", puts=[tail_put]),
    )
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 500)],
        "BBB": [_stock_position("BBB", 300)],
        "CCC": [tail_put],
    }
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
        _regime_account_summary("100000"),
        portfolio_positions,
    )

    assert orders == [("AAA", "NYSE", 40), ("BBB", "NYSE", 240)]


@pytest.mark.asyncio
async def test_untracked_box_does_not_offset_tracked_option_exclusion(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.runtime.account.margin_usage = 1.2
    portfolio_manager.config.strategies.regime_rebalance.weight_base = (
        RegimeRebalanceBaseEnum.net_liq_ex_options
    )
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0

    account_summary = {"NetLiquidation": SimpleNamespace(value="100000")}
    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    async def rebalance_snapshot(include_box: bool):
        portfolio_positions = {
            "AAA": [
                _stock_position("AAA", 540),
                _option_position("AAA", 1, market_value=6000.0),
            ],
            "BBB": [
                _stock_position("BBB", 540),
                _option_position("BBB", 1, market_value=4000.0),
            ],
        }
        if include_box:
            portfolio_positions["SPX"] = [
                _option_position(
                    "SPX",
                    -1,
                    market_value=-1_200_000.0,
                    strike=5000.0,
                    right="C",
                    expiry="20260716",
                ),
                _option_position(
                    "SPX",
                    1,
                    market_value=2000.0,
                    strike=5000.0,
                    right="P",
                    expiry="20260716",
                ),
                _option_position(
                    "SPX",
                    1,
                    market_value=700_000.0,
                    strike=5100.0,
                    right="C",
                    expiry="20260716",
                ),
                _option_position(
                    "SPX",
                    -1,
                    market_value=-2000.0,
                    strike=5100.0,
                    right="P",
                    expiry="20260716",
                ),
            ]

        (
            _,
            orders,
        ) = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )
        gate = portfolio_manager.data_store.get_last_event_payload(
            "regime_rebalance_gate"
        )
        summary = portfolio_manager.data_store.get_last_event_payload(
            "regime_rebalance_summary"
        )
        return {
            "rebalance_base": gate["flow"]["rebalance_base"],
            "current_weights": {
                item["symbol"]: item["current_weight"] for item in summary["summary"]
            },
            "soft_breach": gate["soft_breach"],
            "hard_breach": gate["hard_breach"],
            "orders": orders,
        }

    without_box = await rebalance_snapshot(include_box=False)
    with_large_untracked_box = await rebalance_snapshot(include_box=True)

    assert with_large_untracked_box == without_box
    assert without_box["rebalance_base"] == 108_000
    assert without_box["current_weights"] == {
        "AAA": pytest.approx(0.5),
        "BBB": pytest.approx(0.5),
    }
    assert without_box["soft_breach"] is False
    assert without_box["hard_breach"] is False
    assert without_box["orders"] == []


@pytest.mark.asyncio
async def test_zero_weight_option_does_not_reduce_net_liq_base(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.runtime.account.margin_usage = 1.2
    portfolio_manager.config.strategies.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.strategies.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.portfolio.symbols["CCC"] = SimpleNamespace(
        weight=0.0,
        primary_exchange="NYSE",
    )
    portfolio_manager.config.strategies.regime_rebalance.symbols.append("CCC")

    account_summary = {"NetLiquidation": SimpleNamespace(value="100000")}
    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    async def rebalance_snapshot(include_zero_weight_option: bool):
        portfolio_positions = {
            "AAA": [_stock_position("AAA", 600)],
            "BBB": [_stock_position("BBB", 600)],
        }
        if include_zero_weight_option:
            portfolio_positions["CCC"] = [
                _option_position("CCC", 1, market_value=50_000.0)
            ]

        (
            _,
            orders,
        ) = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )
        gate = portfolio_manager.data_store.get_last_event_payload(
            "regime_rebalance_gate"
        )
        summary = portfolio_manager.data_store.get_last_event_payload(
            "regime_rebalance_summary"
        )
        return {
            "rebalance_base": gate["flow"]["rebalance_base"],
            "current_weights": {
                item["symbol"]: item["current_weight"] for item in summary["summary"]
            },
            "soft_breach": gate["soft_breach"],
            "hard_breach": gate["hard_breach"],
            "orders": orders,
        }

    without_zero_weight_option = await rebalance_snapshot(
        include_zero_weight_option=False
    )
    with_zero_weight_option = await rebalance_snapshot(include_zero_weight_option=True)

    assert with_zero_weight_option == without_zero_weight_option
    assert without_zero_weight_option["rebalance_base"] == 120_000
    assert without_zero_weight_option["current_weights"] == {
        "AAA": pytest.approx(0.5),
        "BBB": pytest.approx(0.5),
    }
    assert without_zero_weight_option["soft_breach"] is False
    assert without_zero_weight_option["hard_breach"] is False
    assert without_zero_weight_option["orders"] == []


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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    last_rebalance = (
        await portfolio_manager.regime_engine._get_last_regime_rebalance_time(
            ["AAA", "BBB"]
        )
    )

    assert last_rebalance == datetime(2024, 1, 7)
    exec_filter = portfolio_manager.ibkr.request_executions.await_args.args[0]
    assert exec_filter.acctCode == "TEST123"


@pytest.mark.asyncio
async def test_regime_rebalance_uses_db_for_cooldown(
    portfolio_manager_with_db, mocker, monkeypatch
):
    fills = [
        SimpleNamespace(
            execution=SimpleNamespace(
                execId="1",
                acctNumber="TEST123",
                orderRef="tg:regime-rebalance:AAA",
                time=datetime(2024, 1, 5, 12, 0, 0),
            ),
            contract=SimpleNamespace(symbol="AAA"),
            time=datetime(2024, 1, 5, 12, 0, 0),
        ),
        SimpleNamespace(
            execution=SimpleNamespace(
                execId="2",
                acctNumber="TEST123",
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

    last_rebalance = (
        await portfolio_manager_with_db.regime_engine._get_last_regime_rebalance_time(
            ["AAA", "BBB"]
        )
    )

    assert last_rebalance == datetime(2024, 1, 7, 12, 0, 0)


@pytest.mark.asyncio
async def test_regime_cooldown_uses_live_fill_when_persistence_drops_it(
    portfolio_manager_with_db, mocker, monkeypatch
):
    portfolio_manager = portfolio_manager_with_db
    _freeze_now(monkeypatch, datetime(2024, 1, 10, 12))
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            execId="live-only",
            acctNumber="TEST123",
            orderRef="tg:regime-rebalance:AAA",
            time=datetime(2024, 1, 9, 12),
        ),
        contract=SimpleNamespace(symbol="AAA"),
        time=datetime(2024, 1, 9, 12),
    )
    portfolio_manager.ibkr.ib.reqExecutionsAsync = mocker.AsyncMock(return_value=[fill])
    record_executions = mocker.patch.object(
        portfolio_manager.data_store,
        "record_executions",
        return_value=None,
    )

    last_rebalance = (
        await portfolio_manager.regime_engine._get_last_regime_rebalance_time(["AAA"])
    )

    assert last_rebalance == datetime(2024, 1, 9, 12)
    record_executions.assert_called_once_with([fill])


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_fails", [False, True])
async def test_regime_cooldown_uses_legacy_history_without_scoped_rows(
    portfolio_manager_with_db, mocker, monkeypatch, refresh_fails
):
    portfolio_manager = portfolio_manager_with_db
    _freeze_now(monkeypatch, datetime(2024, 1, 10, 12))
    with portfolio_manager.data_store.session_scope() as session:
        session.add(
            ExecutionRecord(
                run_id=portfolio_manager.data_store.run_id,
                exec_id="legacy",
                account=None,
                order_ref="tg:regime-rebalance:AAA",
                symbol="AAA",
                execution_time=datetime(2024, 1, 9, 12),
            )
        )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(
        side_effect=ConnectionError("offline") if refresh_fails else None,
        return_value=[],
    )

    last_rebalance = (
        await portfolio_manager.regime_engine._get_last_regime_rebalance_time(["AAA"])
    )

    assert last_rebalance == datetime(2024, 1, 9, 12)


@pytest.mark.asyncio
async def test_regime_cooldown_fails_closed_when_history_is_unavailable(
    portfolio_manager_with_db, mocker, monkeypatch
):
    fixed = datetime(2024, 1, 10, 12)
    _freeze_now(monkeypatch, fixed)
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        side_effect=ConnectionError("offline")
    )

    last_rebalance = (
        await portfolio_manager_with_db.regime_engine._get_last_regime_rebalance_time(
            ["AAA"]
        )
    )

    assert last_rebalance == fixed


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
        await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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
    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
        account_summary, below_band_positions
    )
    assert orders == []

    _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=50.0)
    at_band_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=12)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=16)],
    }
    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    (
        _,
        orders,
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    (
        _,
        orders,
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    (
        _,
        orders,
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    (
        _,
        orders,
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    (
        _,
        orders,
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    (
        _,
        orders,
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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
        await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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
        await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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
        await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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
        await portfolio_manager.regime_engine.check_regime_rebalance_positions(
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
        await portfolio_manager.regime_engine.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )


def _configure_tail_harvest(
    portfolio_manager,
    *symbols: str,
) -> None:
    portfolio_manager.config.strategies.tail_hedge = SimpleNamespace(
        enabled=True,
        targets=[_tail_target(symbol) for symbol in symbols],
    )
    _enable_tail_hedge_stage(portfolio_manager)


def _set_live_tail_positions(
    portfolio_manager, positions, *, cash: float = 0.0
) -> None:
    puts_by_symbol: dict[str, list[SimpleNamespace]] = {}
    for position in positions:
        contract = getattr(position, "contract", None)
        if (
            isinstance(contract, Option)
            and contract.right.upper().startswith("P")
            and float(position.position) > 0
        ):
            puts_by_symbol.setdefault(contract.symbol, []).append(position)
    _save_tail_state(
        portfolio_manager,
        *(
            _tail_state(symbol=symbol, puts=symbol_puts)
            for symbol, symbol_puts in sorted(puts_by_symbol.items())
        ),
    )
    portfolio_manager.ibkr.ib.portfolio.return_value = list(positions)
    portfolio_manager.ibkr.ib.openTrades.return_value = []
    portfolio_manager.ibkr.ib.accountValues.return_value = [
        AccountValue("TEST123", "TotalCashValue", str(cash), "BASE", "")
    ]
    portfolio_manager.ibkr.ib.accountSummaryAsync = AsyncMock(return_value=[])


def _set_tail_quotes(portfolio_manager, mocker, prices: dict[int, float]) -> None:
    async def quote(contract, **_kwargs):
        return _option_ticker(prices[int(contract.conId)])

    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(side_effect=quote)


@pytest.mark.asyncio
async def test_harvest_persists_recovery_intent(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    tail_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    payload = _tail_state(symbol="BBB", puts=[tail_put])
    _save_tail_state(portfolio_manager, payload)
    _set_live_tail_positions(portfolio_manager, [tail_put])
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions={"BBB": [tail_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=payload.open_cohorts,
    )

    assert orders == []
    queued_order = portfolio_manager.orders.records()[0][1]
    assert getattr(queued_order, TAIL_HEDGE_MIN_LIMIT_PRICE_ATTR) == pytest.approx(0.51)
    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None
    state = store.load()
    assert state.open_cohorts[0].pending_recovery_quantity == 1
    assert state.open_cohorts[0].pending_recovery_per_contract == 51.0
    assert isinstance(state.open_cohorts[0].pending_recovery_enqueued_at, datetime)
    assert state.open_cohorts[0].pending_recovery_initial_quantity == 1
    telemetry = portfolio_manager.data_store.get_last_event_payload(
        TAIL_HEDGE_HARVEST_EVENT,
        symbol="BBB",
    )
    assert telemetry is not None
    assert telemetry["outcome"] == "harvest_enqueued"
    assert telemetry["gross_proceeds"] == 120.0
    assert telemetry["estimated_fees"] == 0.0
    assert telemetry["net_proceeds"] == 120.0
    assert telemetry["required_proceeds"] == 100.0
    assert telemetry["excess_proceeds"] == 20.0


@pytest.mark.asyncio
async def test_pending_recovery_serializes_same_symbol_harvests(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    first_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    next_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261218",
        con_id=802,
        average_cost=50.0,
    )
    first_put.contract.multiplier = next_put.contract.multiplier = "100"
    state = _tail_state(symbol="BBB", puts=[first_put, next_put])
    state.cohorts[0].begin_recovery(
        quantity=1,
        proceeds_per_contract=120.0,
        enqueued_at=datetime.now() - timedelta(minutes=10),
    )
    _save_tail_state(portfolio_manager, state)
    portfolio_manager.ibkr.ib.portfolio.return_value = [next_put]
    portfolio_manager.ibkr.ib.openTrades.return_value = []
    portfolio_manager.ibkr.ib.accountValues.return_value = [
        AccountValue("TEST123", "TotalCashValue", "0", "BASE", "")
    ]
    _set_tail_quotes(portfolio_manager, mocker, {802: 1.20})

    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None
    first_orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions={"BBB": [next_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=store.load().open_cohorts,
    )

    assert first_orders == []
    assert portfolio_manager.orders.records() == []
    telemetry = portfolio_manager.data_store.get_last_event_payload(
        TAIL_HEDGE_HARVEST_EVENT,
        symbol="BBB",
    )
    assert telemetry is not None
    assert telemetry["outcome"] == "harvest_deferred"
    assert telemetry["reason"] == "pending_recovery"

    reconciled = store.load()
    portfolio_manager.post_engine.tail_hedge_engine._reconcile_state(
        state=reconciled,
        entry_trades=[],
        account_trades=[],
        pending_close_con_ids=set(),
    )
    assert {cohort.con_id for cohort in store.load().open_cohorts} == {802}

    next_orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions={"BBB": [next_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=store.load().open_cohorts,
    )

    assert next_orders == []
    assert [
        contract.conId
        for contract, _order, _intent_id in portfolio_manager.orders.records()
    ] == [802]


@pytest.mark.asyncio
async def test_harvest_profitability_includes_estimated_broker_fee(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    portfolio_manager.config.runtime.orders.estimated_fee_per_contract = 1.0
    tail_put = _option_position(
        "BBB",
        1,
        market_value=51.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    _set_live_tail_positions(portfolio_manager, [tail_put])
    _set_tail_quotes(portfolio_manager, mocker, {801: 0.51})
    record_harvest = mocker.spy(
        portfolio_manager.regime_engine,
        "_record_tail_harvest",
    )

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions={"BBB": [tail_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []
    assert any(
        call.args[0] == "candidate_not_net_profitable"
        for call in record_harvest.call_args_list
    )


@pytest.mark.asyncio
async def test_harvest_does_not_credit_a_pre_submission_position_change(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    stale_put = _option_position(
        "BBB",
        3,
        market_value=360.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    live_put = _option_position(
        "BBB",
        2,
        market_value=240.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    stale_put.contract.multiplier = "100"
    live_put.contract.multiplier = "100"
    payload = _tail_state(symbol="BBB", puts=[stale_put])
    _save_tail_state(portfolio_manager, payload)
    portfolio_manager.ibkr.ib.portfolio.return_value = [live_put]
    portfolio_manager.ibkr.ib.openTrades.return_value = []
    portfolio_manager.ibkr.ib.accountValues.return_value = [
        AccountValue("TEST123", "TotalCashValue", "0", "BASE", "")
    ]
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions={"BBB": [live_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=payload.open_cohorts,
    )

    assert orders == []
    await portfolio_manager.post_engine.tail_hedge_engine.manage(
        {"BBB": [live_put]},
        net_liquidation=200.0,
    )

    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None
    cohort = store.load().open_cohorts[0]
    assert cohort.quantity == 2
    assert cohort.recovered_cost == 0.0
    assert cohort.pending_recovery_quantity == 1


@pytest.mark.asyncio
async def test_harvest_requires_fresh_persisted_ownership(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    tail_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    stale_state = _tail_state(symbol="BBB", puts=[tail_put])
    _set_live_tail_positions(portfolio_manager, [tail_put])
    _save_tail_state(portfolio_manager)
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions={"BBB": [tail_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=stale_state.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []


@pytest.mark.asyncio
async def test_harvest_does_not_queue_sell_when_recovery_intent_cannot_persist(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    tail_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    payload = _tail_state(symbol="BBB", puts=[tail_put])
    _set_live_tail_positions(portfolio_manager, [tail_put])
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20})
    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None
    mocker.patch.object(
        store,
        "save",
        side_effect=RuntimeError("Failed to persist required tail-hedge state"),
    )

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions={"BBB": [tail_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=payload.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []


def test_partial_broker_working_buy_reserves_only_remaining_debit(
    portfolio_manager_with_db,
):
    portfolio_manager = portfolio_manager_with_db
    working_put = Option("AAA", "20270115", 50.0, "P", "SMART")
    working_put.multiplier = "100"
    portfolio_manager.ibkr.ib.openTrades.return_value = [
        SimpleNamespace(
            contract=working_put,
            order=SimpleNamespace(
                account="TEST123",
                action="BUY",
                lmtPrice=1.0,
                totalQuantity=1,
            ),
            orderStatus=SimpleNamespace(remaining=0.25),
            isDone=lambda: False,
        )
    ]

    shortfall = portfolio_manager.regime_engine._ordinary_rebalance_shortfall(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("500", cash="100"),
        portfolio_positions={},
        market_prices={"BBB": 100.0},
    )

    assert shortfall == 25.0


@pytest.mark.asyncio
async def test_ambiguous_working_buy_skips_tail_harvest(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    tail_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    _set_live_tail_positions(portfolio_manager, [tail_put])
    portfolio_manager.ibkr.ib.openTrades.return_value = [
        SimpleNamespace(
            contract=Stock("AAA", "SMART", "USD"),
            order=SimpleNamespace(
                account="TEST123",
                action="BUY",
                lmtPrice=100.0,
                totalQuantity=1,
            ),
            orderStatus=SimpleNamespace(remaining=0),
            isDone=lambda: False,
        )
    ]
    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock()

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions={"BBB": [tail_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []


@pytest.mark.asyncio
async def test_market_buy_skips_tail_harvest(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    tail_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    _set_live_tail_positions(portfolio_manager, [tail_put])
    portfolio_manager.ibkr.ib.openTrades.return_value = [
        SimpleNamespace(
            contract=Stock("AAA", "SMART", "USD"),
            order=MarketOrder("BUY", 1, account="TEST123"),
            orderStatus=SimpleNamespace(remaining=1),
            isDone=lambda: False,
        )
    ]
    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock()

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions={"BBB": [tail_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []
    portfolio_manager.ibkr.get_ticker_for_contract.assert_not_awaited()


@pytest.mark.asyncio
async def test_harvest_rereads_replacement_ib_async_cache_objects(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    _enable_cash_management_stage(portfolio_manager)
    portfolio_manager.config.strategies.cash_management = SimpleNamespace(
        enabled=True,
        cash_fund="SHV",
        target_cash_balance=0.0,
    )

    live_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=802,
        average_cost=50.0,
    )
    live_put.contract.multiplier = "100"
    live_state = _tail_state(symbol="BBB", puts=[live_put])
    _save_tail_state(portfolio_manager, live_state)
    stale_put = _option_position(
        "BBB",
        1,
        market_value=60.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    cash_fund = _stock_position("SHV", 1, market_value=50.0)
    cash_fund.contract.conId = 901

    live_ib = IB()
    live_ib.wrapper.accountValues[("TEST123", "TotalCashValue", "BASE", "")] = (
        AccountValue("TEST123", "TotalCashValue", "50", "BASE", "")
    )
    live_ib.wrapper.portfolio["TEST123"][802] = live_put
    live_ib.wrapper.portfolio["TEST123"][901] = cash_fund
    account_values = mocker.spy(live_ib, "accountValues")
    account_summary_async = mocker.spy(live_ib, "accountSummaryAsync")
    request_positions = mocker.spy(live_ib, "reqPositionsAsync")
    portfolio_manager.ibkr.ib = live_ib
    _set_tail_quotes(portfolio_manager, mocker, {802: 1.20})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 2)],
        account_summary=_regime_account_summary("500", cash="0"),
        portfolio_positions={"BBB": [stale_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=live_state.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert [
        contract.conId
        for contract, _order, _intent_id in portfolio_manager.orders.records()
    ] == [802]
    account_values.assert_called_once_with("TEST123")
    account_summary_async.assert_not_called()
    request_positions.assert_not_called()


@pytest.mark.asyncio
async def test_harvest_stages_one_shortest_tranche_during_large_shortfall(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    short_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    later_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261218",
        con_id=802,
        average_cost=50.0,
    )
    latest_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20270115",
        con_id=803,
        average_cost=50.0,
    )
    for position in (short_put, later_put, latest_put):
        position.contract.multiplier = "100"
    positions = {"BBB": [short_put, later_put, latest_put]}
    _set_live_tail_positions(portfolio_manager, positions["BBB"])
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20, 802: 1.20, 803: 1.20})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 3)],
        account_summary=_regime_account_summary("500", cash="0"),
        portfolio_positions=positions,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(
            symbol="BBB",
            puts=[latest_put, later_put, short_put],
        ).open_cohorts,
    )

    assert orders == []
    queued = portfolio_manager.orders.records()
    assert [contract.conId for contract, _order, _intent_id in queued] == [801]
    assert [int(order.totalQuantity) for _contract, order, _intent_id in queued] == [1]
    assert all(
        order.orderRef.startswith(f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:BBB:")
        for _contract, order, _intent_id in queued
    )


@pytest.mark.asyncio
async def test_harvest_defers_all_unfunded_shares_when_tranche_covers_only_part(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    tail_put = _option_position(
        "BBB",
        6,
        market_value=600.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    _set_live_tail_positions(portfolio_manager, [tail_put], cash=200.0)
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.0})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 20)],
        account_summary=_regime_account_summary("2500", cash="200"),
        portfolio_positions={"BBB": [tail_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 2)]
    assert [
        int(order.totalQuantity)
        for _contract, order, _intent_id in portfolio_manager.orders.records()
    ] == [6]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "first_price",
        "next_cash",
        "still_hard",
        "expected_orders",
        "expected_sale",
    ),
    [
        (3.0, "300", True, [("BBB", "NYSE", 3)], []),
        (1.0, "100", True, [("BBB", "NYSE", 1)], [802]),
        (1.0, "0", False, [("BBB", "NYSE", 3)], []),
    ],
)
async def test_harvest_next_run_uses_cash_then_rechecks_remaining_shortfall(
    portfolio_manager_with_db,
    mocker,
    first_price,
    next_cash,
    still_hard,
    expected_orders,
    expected_sale,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    first_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    next_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261218",
        con_id=802,
        average_cost=50.0,
    )
    first_put.contract.multiplier = next_put.contract.multiplier = "100"
    cohorts = _tail_state(symbol="BBB", puts=[first_put, next_put]).open_cohorts
    _set_live_tail_positions(portfolio_manager, [first_put, next_put])
    _set_tail_quotes(portfolio_manager, mocker, {801: first_price, 802: 1.2})

    first_orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 3)],
        account_summary=_regime_account_summary("500", cash="0"),
        portfolio_positions={"BBB": [first_put, next_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=cohorts,
    )
    assert first_orders == []
    assert [
        contract.conId
        for contract, _order, _intent_id in portfolio_manager.orders.records()
    ] == [801]

    portfolio_manager.orders.records().clear()
    _set_live_tail_positions(portfolio_manager, [next_put], cash=float(next_cash))
    next_orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 3)],
        account_summary=_regime_account_summary("500", cash=next_cash),
        portfolio_positions={"BBB": [next_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"} if still_hard else set(),
        cohorts=cohorts,
    )

    assert next_orders == expected_orders
    assert [
        contract.conId
        for contract, _order, _intent_id in portfolio_manager.orders.records()
    ] == expected_sale


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "cash_fund_value",
        "sell_threshold",
        "cash_stage_enabled",
        "expected_stock_quantity",
        "put_sales",
    ),
    [
        (250.0, 100.0, True, 2, 0),
        (50.0, 100.0, True, 1, 1),
        (50.0, 200.0, True, 2, 0),
        (250.0, 100.0, False, 0, 2),
    ],
)
async def test_cash_fund_and_tolerance_bound_harvest_shortfall(
    portfolio_manager_with_db,
    mocker,
    cash_fund_value,
    sell_threshold,
    cash_stage_enabled,
    expected_stock_quantity,
    put_sales,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    _enable_cash_management_stage(portfolio_manager, cash_stage_enabled)
    portfolio_manager.config.strategies.cash_management = SimpleNamespace(
        enabled=True,
        cash_fund="SHV",
        target_cash_balance=50.0,
        sell_threshold=sell_threshold,
    )
    tail_put = _option_position(
        "BBB",
        2,
        market_value=240.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    positions = {
        "BBB": [tail_put],
        "SHV": [_stock_position("SHV", 2, market_value=cash_fund_value)],
    }
    _set_live_tail_positions(
        portfolio_manager,
        [
            position
            for symbol_positions in positions.values()
            for position in symbol_positions
        ],
    )
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 2)],
        account_summary=_regime_account_summary("500", cash="0"),
        portfolio_positions=positions,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    expected_orders = (
        [("BBB", "NYSE", expected_stock_quantity)] if expected_stock_quantity else []
    )
    assert orders == expected_orders
    queued_harvests = [
        order
        for contract, order, _intent_id in portfolio_manager.orders.records()
        if isinstance(contract, Option)
    ]
    assert sum(int(order.totalQuantity) for order in queued_harvests) == put_sales


@pytest.mark.asyncio
async def test_cash_fund_capacity_excludes_fractional_unsellable_share(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    _enable_cash_management_stage(portfolio_manager)
    portfolio_manager.config.strategies.cash_management = SimpleNamespace(
        enabled=True,
        cash_fund="SHV",
        target_cash_balance=0.0,
        sell_threshold=0.0,
    )
    tail_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    cash_fund = _stock_position("SHV", 1.5, market_value=150.0)
    positions = {"BBB": [tail_put], "SHV": [cash_fund]}
    _set_live_tail_positions(portfolio_manager, [tail_put, cash_fund], cash=50.0)
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.2})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 2)],
        account_summary=_regime_account_summary("500", cash="50"),
        portfolio_positions=positions,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert len(portfolio_manager.orders.records()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "order_ref"),
    [
        ("broker", f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:BBB:801"),
        ("broker", TAIL_HEDGE_CLOSE_ORDER_REF),
        ("local", TAIL_HEDGE_CLOSE_ORDER_REF),
    ],
)
async def test_working_tail_sale_keeps_unfunded_shares_deferred(
    portfolio_manager_with_db,
    mocker,
    source,
    order_ref,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    tail_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    _set_live_tail_positions(portfolio_manager, [tail_put], cash=100.0)
    working_order = SimpleNamespace(
        account="TEST123",
        action="SELL",
        lmtPrice=1.2,
        totalQuantity=1,
        orderRef=order_ref,
    )
    if source == "broker":
        portfolio_manager.ibkr.ib.openTrades.return_value = [
            SimpleNamespace(
                contract=tail_put.contract,
                order=working_order,
                isDone=lambda: False,
            )
        ]
    else:
        portfolio_manager.orders.add_order(tail_put.contract, working_order, None)
    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock()

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 2)],
        account_summary=_regime_account_summary("500", cash="100"),
        portfolio_positions={"BBB": [tail_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert len(portfolio_manager.orders.records()) == (1 if source == "local" else 0)
    portfolio_manager.ibkr.get_ticker_for_contract.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry_limit_price", "expected_orders", "put_sales"),
    [
        (0.5, [], 1),
        (float("nan"), [("BBB", "NYSE", 1)], 0),
    ],
)
async def test_nan_live_cost_uses_only_a_finite_entry_limit_basis(
    portfolio_manager_with_db,
    mocker,
    entry_limit_price,
    expected_orders,
    put_sales,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    tail_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=float("nan"),
    )
    tail_put.contract.multiplier = "100"
    cohorts = _tail_state(symbol="BBB", puts=[tail_put]).open_cohorts
    cohorts[0].entry_limit_price = entry_limit_price
    _set_live_tail_positions(portfolio_manager, [tail_put])
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions={"BBB": [tail_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=cohorts,
    )

    assert orders == expected_orders
    assert len(portfolio_manager.orders.records()) == put_sales


@pytest.mark.asyncio
async def test_harvest_keeps_buy_when_puts_cannot_fund_one_whole_share(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    tail_put = _option_position(
        "BBB",
        1,
        market_value=60.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    _set_live_tail_positions(portfolio_manager, [tail_put])
    _set_tail_quotes(portfolio_manager, mocker, {801: 0.60})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions={"BBB": [tail_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []


@pytest.mark.asyncio
async def test_harvest_skips_too_small_shortest_tranche_for_later_useful_one(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    shortest_put = _option_position(
        "BBB",
        1,
        market_value=60.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    later_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261218",
        con_id=802,
        average_cost=50.0,
    )
    shortest_put.contract.multiplier = later_put.contract.multiplier = "100"
    positions = {"BBB": [shortest_put, later_put]}
    _set_live_tail_positions(portfolio_manager, positions["BBB"])
    _set_tail_quotes(portfolio_manager, mocker, {801: 0.6, 802: 1.2})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions=positions,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[shortest_put, later_put]).open_cohorts,
    )

    assert orders == []
    assert [
        contract.conId
        for contract, _order, _intent_id in portfolio_manager.orders.records()
    ] == [802]


@pytest.mark.asyncio
async def test_harvest_rounding_cannot_over_defer_across_symbols(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "AAA", "BBB")
    aaa_put = _option_position(
        "AAA",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    bbb_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=802,
        average_cost=50.0,
    )
    aaa_put.contract.multiplier = bbb_put.contract.multiplier = "100"
    _set_live_tail_positions(portfolio_manager, [aaa_put, bbb_put], cash=150.0)
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20, 802: 1.20})
    cohorts = [
        *_tail_state(symbol="AAA", puts=[aaa_put]).open_cohorts,
        *_tail_state(symbol="BBB", puts=[bbb_put]).open_cohorts,
    ]

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("AAA", "NYSE", 1), ("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("500", cash="150"),
        portfolio_positions={"AAA": [aaa_put], "BBB": [bbb_put]},
        market_prices={"AAA": 100.0, "BBB": 100.0},
        regime_summary=[{"symbol": "AAA"}, {"symbol": "BBB"}],
        hard_underweight_symbols={"AAA", "BBB"},
        cohorts=cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert [
        contract.symbol
        for contract, _order, _intent_id in portfolio_manager.orders.records()
    ] == ["AAA"]
    portfolio_manager.ibkr.get_ticker_for_contract.assert_awaited_once()


@pytest.mark.asyncio
async def test_harvest_requires_same_symbol_hard_underweight(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    tail_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    _set_live_tail_positions(portfolio_manager, [tail_put])
    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock()

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        account_summary=_regime_account_summary("200", cash="0"),
        portfolio_positions={"BBB": [tail_put]},
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"AAA"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []
    portfolio_manager.ibkr.get_ticker_for_contract.assert_not_awaited()


@pytest.mark.asyncio
async def test_volatility_sizing_failure_blocks_harvest_but_not_regime_order(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    portfolio_manager.config.portfolio.symbols[
        "BBB"
    ].volatility_weight = _volatility_weight(
        lookback_days=5,
        min_weight=0.10,
        max_weight=0.25,
    )
    tail_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    _save_tail_state(
        portfolio_manager,
        _tail_state(symbol="BBB", puts=[tail_put]),
    )
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 3)],
        "BBB": [_stock_position("BBB", 0), tail_put],
        "OTHER": [_stock_position("OTHER", 1, market_value=100.0)],
    }
    _mock_regime_tickers(portfolio_manager, mocker)
    closes = [100.0, 110.0, 100.0, 110.0]
    dates = _required_regime_history_dates(len(closes))

    async def aligned_closes(symbols, lookback_days, _cooldown_days):
        if symbols == ["BBB"] and lookback_days == 5:
            raise RuntimeError("volatility history unavailable")
        return dates, {symbol: closes for symbol in symbols}

    portfolio_manager.regime_engine._get_regime_aligned_closes = mocker.AsyncMock(
        side_effect=aligned_closes
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    portfolio_manager.ibkr.ib.openTrades.return_value = []
    portfolio_manager.ibkr.ib.portfolio.return_value = [
        position for positions in portfolio_positions.values() for position in positions
    ]
    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock()

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
        _regime_account_summary("580"),
        portfolio_positions,
    )

    assert orders == [("AAA", "NYSE", -1), ("BBB", "NYSE", 2)]
    assert portfolio_manager.orders.records() == []
    portfolio_manager.ibkr.get_ticker_for_contract.assert_not_awaited()
