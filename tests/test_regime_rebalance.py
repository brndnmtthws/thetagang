import math
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from ib_async import IB, AccountValue, Option, Stock

import thetagang.portfolio_manager as pm_module
import thetagang.strategies.regime_engine as regime_engine_module
from thetagang.accounting import BrokerAccountSnapshot
from thetagang.config import Config
from thetagang.db import DataStore, ExecutionRecord
from thetagang.external_decisions import (
    ExternalDecisionRequest,
    ExternalDecisionResponse,
)
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
    RegimeHistoryCache,
)
from thetagang.strategies.tail_hedge_state import (
    TAIL_HEDGE_CLOSE_ORDER_REF,
    TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX,
    TAIL_HEDGE_MIN_LIMIT_PRICE_ATTR,
    TailHedgeCohort,
    TailHedgeState,
)
from thetagang.target_weight_policy import TARGET_WEIGHT_POLICY_STATE_EVENT


def _naive_utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    """Build the naive UTC values used by persisted broker state."""
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC).replace(
        tzinfo=None
    )


REGIME_HISTORY_START = _naive_utc(2024, 1, 2)
REGIME_SYMBOLS = ("AAA", "BBB")


class _FixedTargetWeightProvider:
    def __init__(
        self,
        multiplier: float,
        symbols: tuple[str, ...] = ("AAA",),
        expires_at: datetime | None = None,
    ) -> None:
        self.multiplier = multiplier
        self.symbols = symbols
        self.expires_at = expires_at
        self.requests: list[ExternalDecisionRequest] = []

    async def decide(
        self, request: ExternalDecisionRequest
    ) -> ExternalDecisionResponse:
        self.requests.append(request)
        sessions = request.input["market_data"]["sessions"]
        return ExternalDecisionResponse(
            schema_version=1,
            request_id=request.request_id,
            decision_type=request.decision_type,
            as_of_session=sessions[-1],
            expires_at=self.expires_at,
            producer={"name": "fixture-policy", "version": "model-1"},
            output={
                "adjustments": {
                    symbol: {
                        "multiplier": self.multiplier,
                        "reason": "test-signal",
                    }
                    for symbol in self.symbols
                }
            },
        )


class _FixedTailHarvestProvider:
    def __init__(self, harvest: object, expires_at: datetime | None = None) -> None:
        self.harvest = harvest
        self.expires_at = expires_at
        self.requests: list[ExternalDecisionRequest] = []

    async def decide(
        self, request: ExternalDecisionRequest
    ) -> ExternalDecisionResponse:
        self.requests.append(request)
        sessions = request.input["market_data"]["sessions"]
        return ExternalDecisionResponse(
            schema_version=1,
            request_id=request.request_id,
            decision_type=request.decision_type,
            as_of_session=sessions[-1],
            expires_at=self.expires_at,
            producer={"name": "fixture-harvest", "version": "policy-1"},
            output={"harvest": self.harvest, "reason": "test-signal"},
        )


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


def _stock_position(
    symbol: str,
    position: float,
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
        entered_at = _naive_utc(2026, 1, 1) + timedelta(days=index)
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


def _target_weight_policy(
    *,
    symbols: tuple[str, ...] = ("AAA",),
    min_multiplier: float = 0.8,
    max_multiplier: float = 1.1,
    min_target_weight: float | None = None,
    max_target_weight: float | None = None,
    clamp_to_volatility_bounds: bool = False,
    market_symbols: dict[str, SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=True,
        provider="fixture",
        on_error="baseline",
        max_signal_age_sessions=0,
        max_total_weight=None,
        market_data=SimpleNamespace(
            lookback_days=3,
            include_strategy_symbols=True,
            symbols=market_symbols or {},
        ),
        symbols={
            symbol: SimpleNamespace(
                min_multiplier=min_multiplier,
                max_multiplier=max_multiplier,
                min_target_weight=min_target_weight,
                max_target_weight=max_target_weight,
                clamp_to_volatility_bounds=clamp_to_volatility_bounds,
            )
            for symbol in symbols
        },
    )


def _target_weight_policy_context(
    portfolio_manager,
    *,
    volatility_details: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    return {
        "symbols": list(REGIME_SYMBOLS),
        "symbol_configs": portfolio_manager.config.portfolio.symbols,
        "volatility_details": volatility_details or {},
        "account": BrokerAccountSnapshot(_regime_account_summary("1000")),
        "regime_margin_usage": 1.0,
        "total_value": 1000.0,
        "excluded_value": 0.0,
        "last_rebalance": None,
        "current_positions": {"AAA": 4, "BBB": 5},
        "current_values": {"AAA": 400.0, "BBB": 500.0},
        "market_prices": {"AAA": 100.0, "BBB": 100.0},
    }


def _absolute_trend(
    *,
    lookback_days: int = 168,
    risk_off_multiplier: float = 0.15,
    enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        lookback_days=lookback_days,
        risk_off_multiplier=risk_off_multiplier,
    )


def _absolute_trend_history_cache(
    mocker,
    closes: list[float],
    *,
    lookback_days: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        get=mocker.AsyncMock(
            return_value=(
                _required_regime_history_dates(lookback_days + 1),
                {"AAA": closes},
            )
        )
    )


def _absolute_trend_signal_payload(*, risk_off: bool = True) -> dict[str, object]:
    return {
        "lookback_days": 168,
        "latest_session": "2026-08-21",
        "latest_close": 90.0,
        "moving_average": 100.0,
        "momentum_reference_close": 105.0,
        "lookback_return": 90.0 / 105.0 - 1.0,
        "risk_off": risk_off,
    }


def _ratio_gate_result(
    portfolio_manager,
    *,
    closes_by_symbol,
    ratio_gate,
    effective_weights,
    lookback_days=3,
):
    start_date = _naive_utc(2024, 1, 2)
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
        start_date=_naive_utc(2023, 12, 20),
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
async def test_regime_history_request_covers_200_completed_sessions(
    portfolio_manager, mocker
):
    fixed_now = _naive_utc(2026, 8, 24, 10)
    calendar = regime_engine_module.xcals.get_calendar("XNYS")
    completed_sessions = calendar.sessions[
        calendar.sessions <= regime_engine_module.pd.Timestamp("2026-08-21")
    ][-201:]
    required_dates = [session.date() for session in completed_sessions]
    assert required_dates[0] == date(2025, 11, 3)

    engine = portfolio_manager.regime_engine
    required_dates_mock = mocker.patch.object(
        engine,
        "_get_required_history_dates",
        return_value=required_dates,
    )
    mocker.patch.object(engine, "_now", return_value=fixed_now)
    bars = [
        SimpleNamespace(
            date=datetime.combine(session, datetime.min.time()),
            close=100.0,
        )
        for session in required_dates
    ]
    portfolio_manager.ibkr.request_historical_data = mocker.AsyncMock(return_value=bars)

    dates, aligned_closes = await engine._get_regime_aligned_closes(
        list(REGIME_SYMBOLS),
        lookback_days=200,
        cooldown_days=0,
    )

    required_dates_mock.assert_called_once_with(201)
    assert dates == required_dates
    assert all(len(closes) == 201 for closes in aligned_closes.values())
    assert {
        call.args[1]
        for call in portfolio_manager.ibkr.request_historical_data.await_args_list
    } == {"295 D"}


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
async def test_external_target_weight_policy_adjusts_post_volatility_target(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    regime_rebalance = portfolio_manager.config.strategies.regime_rebalance
    regime_rebalance.soft_band = 0.10
    regime_rebalance.choppiness_min = 0.0
    regime_rebalance.efficiency_max = 1.0
    regime_rebalance.target_weight_policy = _target_weight_policy(
        market_symbols={"QQQ": SimpleNamespace(primary_exchange="NASDAQ")}
    )
    provider = _FixedTargetWeightProvider(0.8)
    portfolio_manager.external_decisions.replace("fixture", provider)

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
        _regime_account_summary("400"),
        _regime_stock_positions(aaa=2, bbb=2),
    )

    assert orders == [("AAA", "NYSE", -1)]
    assert len(provider.requests) == 1
    request_input = provider.requests[0].input
    assert request_input["symbols"]["AAA"]["post_volatility_weight"] == 0.5
    assert request_input["symbols"]["AAA"]["current_weight"] == 0.5
    assert request_input["account"]["rebalance_base_value"] == 400.0
    assert request_input["market_data"]["closes"] == {
        "AAA": [100.0, 110.0, 100.0, 110.0],
        "BBB": [100.0, 110.0, 100.0, 110.0],
        "QQQ": [100.0, 110.0, 100.0, 110.0],
    }
    assert request_input["market_data"]["primary_exchanges"]["QQQ"] == "NASDAQ"
    state = portfolio_manager.data_store.get_last_event_payload(
        TARGET_WEIGHT_POLICY_STATE_EVENT
    )
    assert state["symbols"]["AAA"]["multiplier"] == pytest.approx(0.8)
    assert state["symbols"]["AAA"]["effective_weight"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_external_target_weight_policy_can_target_all_cash(
    portfolio_manager, mocker
):
    regime_rebalance = portfolio_manager.config.strategies.regime_rebalance
    regime_rebalance.soft_band = 0.10
    regime_rebalance.choppiness_min = 0.0
    regime_rebalance.efficiency_max = 1.0
    regime_rebalance.target_weight_policy = _target_weight_policy(
        symbols=REGIME_SYMBOLS,
        min_multiplier=0.0,
        max_multiplier=1.0,
    )
    portfolio_manager.external_decisions.replace(
        "fixture", _FixedTargetWeightProvider(0.0, REGIME_SYMBOLS)
    )
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(
        portfolio_manager,
        mocker,
        [100.0, 110.0, 100.0, 110.0],
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
        _regime_account_summary("400"),
        _regime_stock_positions(aaa=2, bbb=2),
    )

    assert orders == [("AAA", "NYSE", -2), ("BBB", "NYSE", -2)]


@pytest.mark.asyncio
async def test_model_target_bounds_preserve_smoothing_capital_base_and_trend(
    portfolio_manager_with_db: Any, mocker: Any
) -> None:
    manager = portfolio_manager_with_db
    manager.config.runtime.account.margin_usage = 1.25
    regime = manager.config.strategies.regime_rebalance
    regime.weight_base = RegimeRebalanceBaseEnum.net_liq_ex_options
    regime.target_weight_policy = _target_weight_policy(
        min_multiplier=0.5,
        max_multiplier=1.2,
        min_target_weight=0.20,
        max_target_weight=0.55,
        clamp_to_volatility_bounds=False,
    )
    symbol_config = manager.config.portfolio.symbols["AAA"]
    symbol_config.volatility_weight = _volatility_weight(
        target_vol=0.01, min_weight=0.25, max_weight=0.5, smoothing_factor=0.5
    )
    symbol_config.absolute_trend = _absolute_trend(
        lookback_days=3, risk_off_multiplier=0.25
    )
    provider = _FixedTargetWeightProvider(0.5)
    manager.external_decisions.replace("fixture", provider)
    _mock_regime_tickers(manager, mocker)
    _mock_regime_histories(
        manager,
        mocker,
        {"AAA": [100.0, 100.0, 100.0, 90.0], "BBB": [100.0] * 4},
    )
    manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    positions = _regime_stock_positions(aaa=2, bbb=2)
    positions["AAA"].append(_option_position("AAA", 1, market_value=200.0))

    await manager.regime_engine.check_regime_rebalance_positions(
        _regime_account_summary("2000"), positions
    )

    request = provider.requests[0].input
    assert request["account"]["rebalance_base_value"] == pytest.approx(2250.0)
    assert request["adjustment_constraints"]["AAA"] == {
        "min_multiplier": 0.5,
        "max_multiplier": 1.2,
        "min_target_weight": 0.20,
        "max_target_weight": 0.55,
        "clamp_to_volatility_bounds": False,
    }
    # Volatility still smooths 50% toward its 25% floor, producing 37.5%.
    volatility = manager.data_store.get_last_event_payload("volatility_weight_state")
    assert volatility["symbols"]["AAA"]["effective_weight"] == pytest.approx(0.375)
    assert volatility["symbols"]["AAA"]["smoothing_factor"] == 0.5
    assert request["symbols"]["AAA"]["post_volatility_weight"] == pytest.approx(0.375)
    policy = manager.data_store.get_last_event_payload(TARGET_WEIGHT_POLICY_STATE_EVENT)
    assert policy["symbols"]["AAA"]["raw_weight"] == pytest.approx(0.1875)
    assert policy["symbols"]["AAA"]["effective_weight"] == pytest.approx(0.20)
    trend = manager.data_store.get_last_event_payload("absolute_trend_state")
    assert trend["symbols"]["AAA"]["pre_trend_target"] == pytest.approx(0.20)
    assert trend["symbols"]["AAA"]["final_target"] == pytest.approx(0.05)
    assert symbol_config.volatility_weight.min_weight == 0.25
    assert symbol_config.volatility_weight.smoothing_factor == 0.5


@pytest.mark.asyncio
async def test_external_target_weight_policy_reuses_decision_during_replanning(
    portfolio_manager, mocker
):
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight()
    regime_rebalance = portfolio_manager.config.strategies.regime_rebalance
    regime_rebalance.target_weight_policy = _target_weight_policy()
    provider = _FixedTargetWeightProvider(1.07)
    portfolio_manager.external_decisions.replace("fixture", provider)
    history_dates = _required_regime_history_dates(4)
    portfolio_manager.regime_engine._get_regime_aligned_closes = mocker.AsyncMock(
        return_value=(
            history_dates,
            {
                "AAA": [100.0, 101.0, 102.0, 103.0],
                "BBB": [100.0, 100.0, 100.0, 100.0],
            },
        )
    )
    kwargs = _target_weight_policy_context(
        portfolio_manager,
        volatility_details={
            "AAA": {
                "base_weight": 0.5,
                "effective_weight": 0.36,
                "realized_vol": 0.44,
            }
        },
    )

    first, _ = await portfolio_manager.regime_engine._apply_target_weight_policy(
        {"AAA": 0.36, "BBB": 0.45},
        **kwargs,
    )
    second, _ = await portfolio_manager.regime_engine._apply_target_weight_policy(
        {"AAA": 0.40, "BBB": 0.45},
        **kwargs,
    )
    portfolio_manager.regime_engine.begin_run()
    third, _ = await portfolio_manager.regime_engine._apply_target_weight_policy(
        {"AAA": 0.36, "BBB": 0.45},
        **kwargs,
    )

    assert first["AAA"] == pytest.approx(0.3852)
    assert second["AAA"] == pytest.approx(0.428)
    assert third["AAA"] == pytest.approx(0.3852)
    assert len(provider.requests) == 2
    symbol_input = provider.requests[0].input["symbols"]["AAA"]
    assert symbol_input["volatility_weight"]["config"]["target_vol"] == 0.32
    assert symbol_input["volatility_weight"]["calculation"]["realized_vol"] == 0.44
    assert symbol_input["execution_constraints"] == {
        "trading_allowed": True,
        "rebalance_mode": "both",
        "min_threshold_shares": None,
        "min_threshold_amount": None,
        "min_threshold_percent": None,
        "min_threshold_percent_relative": None,
    }
    assert provider.requests[0].input["total_weight_constraint"] == {
        "max_total_weight": None,
        "effective_max_total_weight": 1.0,
        "default_prevents_additional_leverage": True,
    }


def test_external_target_weight_policy_converts_naive_local_time_to_utc(
    portfolio_manager,
) -> None:
    local_time = _naive_utc(2026, 9, 3, 10, 30)

    assert portfolio_manager.regime_engine._as_utc(local_time) == (
        local_time.astimezone(UTC)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("on_error", ["baseline", "abort"])
@pytest.mark.parametrize("initially_accepted", [False, True])
async def test_rejected_target_weights_stay_rejected_until_next_run(
    portfolio_manager: Any,
    mocker: Any,
    on_error: str,
    initially_accepted: bool,
) -> None:
    engine = portfolio_manager.regime_engine
    policy = _target_weight_policy()
    policy.on_error = on_error
    portfolio_manager.config.strategies.regime_rebalance.target_weight_policy = policy
    provider = _FixedTargetWeightProvider(1.1)
    portfolio_manager.external_decisions.replace("fixture", provider)
    engine._get_regime_aligned_closes = mocker.AsyncMock(
        return_value=(
            _required_regime_history_dates(4),
            {"AAA": [100.0] * 4, "BBB": [100.0] * 4},
        )
    )
    context = _target_weight_policy_context(portfolio_manager)
    baseline = {"AAA": 0.4, "BBB": 0.5}
    if initially_accepted:
        weights, _ = await engine._apply_target_weight_policy(baseline, **context)
        assert weights["AAA"] == pytest.approx(0.44)

    # Reject the cached multiplier when its targets exceed the exposure ceiling.
    # A later post-fill replan must retain that failure even if the new baseline
    # would make the same multiplier affordable again.
    for current_baseline in ({"AAA": 0.5, "BBB": 0.5}, baseline):
        if on_error == "abort":
            with pytest.raises(RuntimeError, match="permitted total weight"):
                await engine._apply_target_weight_policy(current_baseline, **context)
        else:
            weights, details = await engine._apply_target_weight_policy(
                current_baseline, **context
            )
            assert weights == current_baseline
            assert details["AAA"]["status"] == "baseline"
            assert details["AAA"]["risk_ready"] is False
    assert len(provider.requests) == 1

    engine.begin_run()
    weights, details = await engine._apply_target_weight_policy(baseline, **context)
    assert weights["AAA"] == pytest.approx(0.44)
    assert details["AAA"]["risk_ready"] is True
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_external_target_weight_policy_checks_expiry_after_provider_returns(
    portfolio_manager, mocker
):
    regime_rebalance = portfolio_manager.config.strategies.regime_rebalance
    regime_rebalance.target_weight_policy = _target_weight_policy()
    request_time = datetime(2026, 9, 3, 14, 30, tzinfo=UTC)
    validation_time = request_time + timedelta(seconds=10)
    provider = _FixedTargetWeightProvider(
        1.07,
        expires_at=request_time + timedelta(seconds=5),
    )
    portfolio_manager.external_decisions.replace("fixture", provider)
    portfolio_manager.regime_engine._get_regime_aligned_closes = mocker.AsyncMock(
        return_value=(
            _required_regime_history_dates(4),
            {
                "AAA": [100.0, 101.0, 102.0, 103.0],
                "BBB": [100.0, 100.0, 100.0, 100.0],
            },
        )
    )
    portfolio_manager.regime_engine._now = mocker.Mock(
        side_effect=[request_time, validation_time]
    )
    baseline = {"AAA": 0.36, "BBB": 0.45}

    (
        adjusted,
        details,
    ) = await portfolio_manager.regime_engine._apply_target_weight_policy(
        baseline,
        **_target_weight_policy_context(portfolio_manager),
    )

    assert provider.requests[0].generated_at == request_time
    assert adjusted == baseline
    assert details["AAA"]["status"] == "baseline"
    assert "signal has expired" in details["AAA"]["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("on_error", ["baseline", "abort"])
async def test_cached_target_weight_decision_expires_during_replanning(
    portfolio_manager: Any, mocker: Any, on_error: str
) -> None:
    engine = portfolio_manager.regime_engine
    policy = _target_weight_policy()
    policy.on_error = on_error
    portfolio_manager.config.strategies.regime_rebalance.target_weight_policy = policy
    now = datetime(2026, 9, 3, 14, 30, tzinfo=UTC)
    engine._now = mocker.Mock(return_value=now)
    provider = _FixedTargetWeightProvider(1.07, expires_at=now + timedelta(seconds=5))
    portfolio_manager.external_decisions.replace("fixture", provider)
    engine._get_regime_aligned_closes = mocker.AsyncMock(
        return_value=(
            _required_regime_history_dates(4),
            {"AAA": [100.0] * 4, "BBB": [100.0] * 4},
        )
    )
    baseline = {"AAA": 0.36, "BBB": 0.45}
    context = _target_weight_policy_context(portfolio_manager)
    first, _ = await engine._apply_target_weight_policy(baseline, **context)
    assert first["AAA"] == pytest.approx(0.3852)

    engine._now.return_value = now + timedelta(seconds=5)
    if on_error == "abort":
        with pytest.raises(RuntimeError, match="expired"):
            await engine._apply_target_weight_policy(baseline, **context)
    else:
        second, details = await engine._apply_target_weight_policy(baseline, **context)
        assert second == baseline
        assert details["AAA"]["risk_ready"] is False
        assert "expired" in details["AAA"]["error"]
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_disabled_target_weight_policy_preserves_harvest_eligibility(
    portfolio_manager: Any, mocker: Any
) -> None:
    regime = portfolio_manager.config.strategies.regime_rebalance
    regime.hard_band = 0.4
    regime.target_weight_policy = _target_weight_policy(symbols=("BBB",))
    regime.target_weight_policy.enabled = False
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])
    apply_harvest = mocker.patch.object(
        portfolio_manager.regime_engine,
        "_apply_tail_harvest",
        new=mocker.AsyncMock(return_value=[]),
    )

    await portfolio_manager.regime_engine.check_regime_rebalance_positions(
        _regime_account_summary(),
        _regime_stock_positions(),
    )

    assert apply_harvest.call_args.kwargs["hard_underweight_symbols"] == {"BBB"}


@pytest.mark.asyncio
async def test_exchange_override_history_does_not_mix_persisted_listings(
    portfolio_manager_with_db: PortfolioManager, mocker: Any, monkeypatch: Any
) -> None:
    manager = portfolio_manager_with_db
    _disable_regime_history_retry_delay(monkeypatch)
    dates = _set_required_regime_history_dates(manager, monkeypatch, required_points=4)
    _seed_regime_history_cache(manager, [100.0] * 4, symbols=("AAA",))
    bars = _regime_bars([200.0] * 4)
    manager.ibkr.ib.reqHistoricalDataAsync = mocker.AsyncMock(return_value=bars)

    _, prices = await manager.regime_engine._get_regime_aligned_closes(
        ["AAA"],
        3,
        0,
        primary_exchanges={"AAA": "NASDAQ"},
    )
    assert prices == {"AAA": [200.0] * 4}

    manager.ibkr.ib.reqHistoricalDataAsync.return_value = []
    # Exercise the persistent fallback, bypassing the per-plan memory cache.
    _, default_prices = await manager.regime_engine._get_regime_aligned_closes(
        ["AAA"],
        3,
        0,
    )
    (
        cached_dates,
        override_prices,
    ) = await manager.regime_engine._get_regime_aligned_closes(
        ["AAA"],
        3,
        0,
        primary_exchanges={"AAA": "NASDAQ"},
    )
    assert default_prices == {"AAA": [100.0] * 4}
    assert cached_dates == dates
    assert override_prices == prices
    with pytest.raises(ValueError, match="aligned history|fresh historical data"):
        await manager.regime_engine._get_regime_aligned_closes(
            ["AAA"],
            3,
            0,
            primary_exchanges={"AAA": "ARCA"},
        )


@pytest.mark.asyncio
async def test_external_target_weight_policy_clamps_to_volatility_bounds(
    portfolio_manager, mocker
):
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(max_weight=0.38)
    regime_rebalance = portfolio_manager.config.strategies.regime_rebalance
    regime_rebalance.target_weight_policy = _target_weight_policy(
        clamp_to_volatility_bounds=True
    )
    provider = _FixedTargetWeightProvider(1.1)
    portfolio_manager.external_decisions.replace("fixture", provider)
    portfolio_manager.regime_engine._get_regime_aligned_closes = mocker.AsyncMock(
        return_value=(
            _required_regime_history_dates(4),
            {
                "AAA": [100.0, 101.0, 102.0, 103.0],
                "BBB": [100.0, 100.0, 100.0, 100.0],
            },
        )
    )

    (
        adjusted,
        details,
    ) = await portfolio_manager.regime_engine._apply_target_weight_policy(
        {"AAA": 0.36, "BBB": 0.45},
        **_target_weight_policy_context(portfolio_manager),
    )

    assert details["AAA"]["raw_weight"] == pytest.approx(0.396)
    assert adjusted["AAA"] == pytest.approx(0.38)


@pytest.mark.asyncio
@pytest.mark.parametrize("clamp", [True, False])
async def test_external_target_weight_policy_caps_before_checking_final_weight(
    portfolio_manager: Any, mocker: Any, clamp: bool
) -> None:
    manager = portfolio_manager
    manager.config.portfolio.symbols["AAA"].volatility_weight = _volatility_weight(
        max_weight=1.0
    )
    manager.config.strategies.regime_rebalance.target_weight_policy = (
        _target_weight_policy(clamp_to_volatility_bounds=clamp)
    )
    manager.external_decisions.replace("fixture", _FixedTargetWeightProvider(1.1))
    manager.regime_engine._get_regime_aligned_closes = mocker.AsyncMock(
        return_value=(
            _required_regime_history_dates(4),
            {"AAA": [100.0] * 4, "BBB": [100.0] * 4},
        )
    )
    baseline = {"AAA": 0.95, "BBB": 0.0}

    weights, details = await manager.regime_engine._apply_target_weight_policy(
        baseline,
        **_target_weight_policy_context(manager),
    )

    if clamp:
        assert weights == {"AAA": 1.0, "BBB": 0.0}
        assert details["AAA"]["raw_weight"] == pytest.approx(1.045)
        assert details["AAA"]["status"] == "applied"
    else:
        assert weights == baseline
        assert details["AAA"]["status"] == "baseline"
        assert "invalid weight" in details["AAA"]["error"]


@pytest.mark.asyncio
async def test_external_target_weight_policy_falls_back_on_invalid_signal(
    portfolio_manager, mocker
):
    regime_rebalance = portfolio_manager.config.strategies.regime_rebalance
    regime_rebalance.target_weight_policy = _target_weight_policy()
    portfolio_manager.external_decisions.replace(
        "fixture", _FixedTargetWeightProvider(1.2)
    )
    portfolio_manager.regime_engine._get_regime_aligned_closes = mocker.AsyncMock(
        return_value=(
            _required_regime_history_dates(4),
            {
                "AAA": [100.0, 101.0, 102.0, 103.0],
                "BBB": [100.0, 100.0, 100.0, 100.0],
            },
        )
    )
    baseline = {"AAA": 0.36, "BBB": 0.45}

    (
        adjusted,
        details,
    ) = await portfolio_manager.regime_engine._apply_target_weight_policy(
        baseline,
        **_target_weight_policy_context(portfolio_manager),
    )

    assert adjusted == baseline
    assert details["AAA"]["status"] == "baseline"
    assert details["AAA"]["multiplier"] == 1.0
    assert details["AAA"]["risk_ready"] is False
    assert "outside configured bounds" in details["AAA"]["error"]


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
async def test_regime_rebalance_volatility_weight_stacks_above_100_on_margin(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.portfolio.symbols["AAA"].weight = 0.6
    portfolio_manager.config.portfolio.symbols["BBB"].weight = 0.4
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.32,
        min_weight=0.25,
        max_weight=0.65,
        smoothing_factor=1.0,
    )
    regime_rebalance = portfolio_manager.config.strategies.regime_rebalance
    regime_rebalance.soft_band = 0.0
    regime_rebalance.choppiness_min = 0.0
    regime_rebalance.efficiency_max = 1.0
    regime_rebalance.deficit_rail_start = 0.01
    regime_rebalance.deficit_rail_stop = 0.01

    account_summary = {"NetLiquidation": SimpleNamespace(value="100000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 600)],
        "BBB": [_stock_position("BBB", 400)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 101.0, 102.0, 103.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == [("AAA", "NYSE", 50)]
    payload = portfolio_manager.data_store.get_last_event_payload(
        "volatility_weight_state"
    )
    assert payload["symbols"]["AAA"]["effective_weight"] == pytest.approx(0.65)
    assert payload["total_effective_weight"] == pytest.approx(1.05)
    assert payload["unallocated_target_weight"] == pytest.approx(0.0)
    assert payload["stacked_target_weight"] == pytest.approx(0.05)
    assert payload["stacked_target_value"] == pytest.approx(5000.0)


@pytest.mark.asyncio
async def test_regime_rebalance_authorized_stack_is_not_a_flow_or_deficit(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    portfolio_manager.config.portfolio.symbols["AAA"].weight = 0.6
    portfolio_manager.config.portfolio.symbols["BBB"].weight = 0.4
    portfolio_manager.config.portfolio.symbols[
        "AAA"
    ].volatility_weight = _volatility_weight(
        target_vol=0.32,
        min_weight=0.25,
        max_weight=0.65,
        smoothing_factor=1.0,
    )
    regime_rebalance = portfolio_manager.config.strategies.regime_rebalance
    regime_rebalance.deficit_rail_start = 0.01
    regime_rebalance.deficit_rail_stop = 0.005

    account_summary = {"NetLiquidation": SimpleNamespace(value="100000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 650)],
        "BBB": [_stock_position("BBB", 400)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 101.0, 102.0, 103.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
        account_summary, portfolio_positions
    )

    assert orders == []
    payload = portfolio_manager.data_store.get_last_event_payload(
        "regime_rebalance_gate"
    )
    assert payload["unallocated_rebalance_capacity"] == pytest.approx(-5000.0)
    assert payload["flow"]["flow_rebalance_capacity"] == pytest.approx(0.0)
    assert payload["flow"]["classification"] == "none"
    assert payload["flow"]["gate"] is False
    assert payload["deficit"]["gate"] is False


@pytest.mark.asyncio
async def test_regime_rebalance_rejects_non_volatility_stack(portfolio_manager, mocker):
    portfolio_manager.config.portfolio.symbols["AAA"].weight = 0.6
    portfolio_manager.config.portfolio.symbols["BBB"].weight = 0.5

    account_summary = {"NetLiquidation": SimpleNamespace(value="1000")}
    portfolio_positions = {
        "AAA": [_stock_position("AAA", 6)],
        "BBB": [_stock_position("BBB", 5)],
    }

    _mock_regime_tickers(portfolio_manager, mocker)
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="Only volatility-adjusted weights"):
        await portfolio_manager.regime_engine.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )


@pytest.mark.asyncio
async def test_regime_rebalance_managed_stocks_rejects_volatility_stack(
    portfolio_manager, mocker
):
    portfolio_manager.config.strategies.regime_rebalance.weight_base = (
        RegimeRebalanceBaseEnum.managed_stocks
    )
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

    with pytest.raises(ValueError, match="weight_base is managed_stocks"):
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


@pytest.mark.parametrize(
    ("closes", "expected_risk_off"),
    [
        pytest.param(
            [100.0, 100.0, 100.0, 99.0],
            True,
            id="below-average-and-reference",
        ),
        pytest.param(
            [110.0, 95.0, 95.0, 100.0],
            False,
            id="equal-average-boundary",
        ),
        pytest.param(
            [100.0, 110.0, 110.0, 100.0],
            False,
            id="equal-reference-boundary",
        ),
    ],
)
@pytest.mark.asyncio
async def test_absolute_trend_risk_boundaries(
    portfolio_manager, mocker, closes, expected_risk_off
):
    portfolio_manager.config.portfolio.symbols["AAA"].absolute_trend = _absolute_trend(
        lookback_days=3
    )
    history_cache = _absolute_trend_history_cache(
        mocker,
        closes,
        lookback_days=3,
    )

    weights, details = await portfolio_manager.regime_engine._apply_absolute_trend(
        {"AAA": 0.4},
        portfolio_manager.config.portfolio.symbols,
        history_cache,
    )

    expected_multiplier = 0.15 if expected_risk_off else 1.0
    assert details["AAA"]["risk_off"] is expected_risk_off
    assert details["AAA"]["applied_multiplier"] == pytest.approx(expected_multiplier)
    assert weights["AAA"] == pytest.approx(0.4 * expected_multiplier)


@pytest.mark.parametrize(
    ("pre_trend_target", "expected_final_target"),
    [(0.30, 0.045), (0.40, 0.06), (0.45, 0.0675)],
)
@pytest.mark.asyncio
async def test_absolute_trend_scales_volatility_targets(
    portfolio_manager, mocker, pre_trend_target, expected_final_target
):
    portfolio_manager.config.portfolio.symbols["AAA"].absolute_trend = _absolute_trend()
    completed_closes = [100.0] * 168 + [90.0]
    history_cache = _absolute_trend_history_cache(
        mocker,
        completed_closes,
        lookback_days=168,
    )

    weights, details = await portfolio_manager.regime_engine._apply_absolute_trend(
        {"AAA": pre_trend_target},
        portfolio_manager.config.portfolio.symbols,
        history_cache,
    )

    history_cache.get.assert_awaited_once_with(["AAA"], 168, 0)
    assert details["AAA"]["pre_trend_target"] == pytest.approx(pre_trend_target)
    assert details["AAA"]["final_target"] == pytest.approx(expected_final_target)
    assert weights["AAA"] == pytest.approx(expected_final_target)


@pytest.mark.asyncio
async def test_absolute_trend_excludes_incomplete_bar(
    portfolio_manager, mocker, monkeypatch
):
    portfolio_manager.config.portfolio.symbols["AAA"].absolute_trend = _absolute_trend()
    required_dates = _set_required_regime_history_dates(
        portfolio_manager, monkeypatch, 169
    )
    completed_closes = [100.0] * 168 + [90.0]
    bars = [
        SimpleNamespace(
            date=datetime.combine(bar_date, datetime.min.time()),
            close=close,
        )
        for bar_date, close in zip(required_dates, completed_closes, strict=True)
    ]
    bars.append(
        SimpleNamespace(
            date=datetime.combine(
                required_dates[-1] + timedelta(days=1), datetime.min.time()
            ),
            close=200.0,
        )
    )
    portfolio_manager.ibkr.request_historical_data = mocker.AsyncMock(return_value=bars)
    aligned_spy = mocker.spy(
        portfolio_manager.regime_engine,
        "_get_regime_aligned_closes",
    )

    _, details = await portfolio_manager.regime_engine._apply_absolute_trend(
        {"AAA": 0.4},
        portfolio_manager.config.portfolio.symbols,
        RegimeHistoryCache(portfolio_manager.regime_engine._get_regime_aligned_closes),
    )

    aligned_spy.assert_awaited_once_with(["AAA"], 168, 0)
    assert details["AAA"]["latest_session"] == str(required_dates[-1])
    assert details["AAA"]["latest_close"] == pytest.approx(90.0)
    assert details["AAA"]["risk_off"] is True


@pytest.mark.asyncio
async def test_regime_history_cache_keys_primary_exchange_overrides(mocker):
    result = ([date(2024, 1, 2)], {"AAA": [100.0]})
    fetcher = mocker.AsyncMock(return_value=result)
    history_cache = RegimeHistoryCache(fetcher)

    assert await history_cache.get(["AAA"], 20, 0) == result
    assert await history_cache.get(["AAA"], 20, 0) == result

    primary_exchanges = {"AAA": "NYSE"}
    assert (
        await history_cache.get(
            ["AAA"],
            20,
            0,
            primary_exchanges=primary_exchanges,
        )
        == result
    )
    assert (
        await history_cache.get(
            ["AAA"],
            20,
            0,
            primary_exchanges=primary_exchanges,
        )
        == result
    )

    assert fetcher.await_count == 2
    fetcher.assert_any_await(["AAA"], 20, 0)
    fetcher.assert_any_await(
        ["AAA"],
        20,
        0,
        primary_exchanges=primary_exchanges,
    )


@pytest.mark.asyncio
async def test_absolute_trend_reuses_persisted_state_when_history_fails(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.portfolio.symbols[
        "AAA"
    ].absolute_trend = _absolute_trend()
    portfolio_manager_with_db.data_store.record_event(
        "absolute_trend_state",
        {"symbols": {"AAA": _absolute_trend_signal_payload()}},
    )
    history_cache = SimpleNamespace(
        get=mocker.AsyncMock(side_effect=TimeoutError("history unavailable"))
    )

    (
        weights,
        details,
    ) = await portfolio_manager_with_db.regime_engine._apply_absolute_trend(
        {"AAA": 0.4},
        portfolio_manager_with_db.config.portfolio.symbols,
        history_cache,
    )

    assert weights["AAA"] == pytest.approx(0.06)
    assert details["AAA"]["risk_off"] is True
    assert details["AAA"]["history_source"] == "persisted"


@pytest.mark.asyncio
async def test_absolute_trend_excludes_current_run_state_when_requested(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.portfolio.symbols[
        "AAA"
    ].absolute_trend = _absolute_trend(lookback_days=3)
    history_cache = _absolute_trend_history_cache(
        mocker,
        [100.0, 100.0, 100.0, 90.0],
        lookback_days=3,
    )
    get_last_event_payload = mocker.spy(
        portfolio_manager_with_db.data_store,
        "get_last_event_payload",
    )

    await portfolio_manager_with_db.regime_engine._apply_absolute_trend(
        {"AAA": 0.4},
        portfolio_manager_with_db.config.portfolio.symbols,
        history_cache,
        exclude_current_run_state=True,
    )

    get_last_event_payload.assert_called_once_with(
        "absolute_trend_state",
        exclude_current_run=True,
        raise_on_error=True,
    )


@pytest.mark.parametrize(
    "persisted_signal",
    [
        pytest.param(None, id="missing"),
        pytest.param(
            _absolute_trend_signal_payload(risk_off=False),
            id="inconsistent-risk-state",
        ),
    ],
)
@pytest.mark.asyncio
async def test_absolute_trend_history_failure_without_valid_state_aborts(
    portfolio_manager_with_db, mocker, persisted_signal
):
    portfolio_manager_with_db.config.portfolio.symbols[
        "AAA"
    ].absolute_trend = _absolute_trend()
    if persisted_signal is not None:
        portfolio_manager_with_db.data_store.record_event(
            "absolute_trend_state",
            {"symbols": {"AAA": persisted_signal}},
        )
    history_cache = SimpleNamespace(
        get=mocker.AsyncMock(side_effect=TimeoutError("history unavailable"))
    )

    with pytest.raises(RuntimeError, match="current history or a valid persisted"):
        await portfolio_manager_with_db.regime_engine._apply_absolute_trend(
            {"AAA": 0.4},
            portfolio_manager_with_db.config.portfolio.symbols,
            history_cache,
        )


@pytest.mark.asyncio
async def test_absolute_trend_invalid_persisted_symbol_map_aborts_cleanly(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.portfolio.symbols[
        "AAA"
    ].absolute_trend = _absolute_trend()
    portfolio_manager_with_db.data_store.record_event(
        "absolute_trend_state",
        {"symbols": []},
    )
    history_cache = SimpleNamespace(
        get=mocker.AsyncMock(side_effect=TimeoutError("history unavailable"))
    )

    with pytest.raises(RuntimeError, match="current history or a valid persisted"):
        await portfolio_manager_with_db.regime_engine._apply_absolute_trend(
            {"AAA": 0.4},
            portfolio_manager_with_db.config.portfolio.symbols,
            history_cache,
        )


@pytest.mark.asyncio
async def test_absolute_trend_preserves_volatility_state_and_forces_hard_band_sell(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.portfolio.symbols[
        "AAA"
    ].absolute_trend = _absolute_trend(lookback_days=3)
    volatility_details = {
        "AAA": {
            "base_weight": 0.5,
            "effective_weight": 0.4,
            "target_weight": 0.4,
            "realized_vol": 0.5,
            "smoothing_factor": 0.3,
        }
    }
    mocker.patch.object(
        portfolio_manager_with_db.regime_engine,
        "_resolve_effective_weights",
        new=mocker.AsyncMock(
            return_value=({"AAA": 0.4, "BBB": 0.5}, volatility_details)
        ),
    )
    _mock_regime_tickers(portfolio_manager_with_db, mocker)
    _mock_regime_histories(
        portfolio_manager_with_db,
        mocker,
        {
            "AAA": [100.0, 100.0, 100.0, 90.0],
            "BBB": [100.0, 100.0, 100.0, 100.0],
        },
    )
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )

    (
        _,
        orders,
    ) = await portfolio_manager_with_db.regime_engine.check_regime_rebalance_positions(
        _regime_account_summary(),
        _regime_stock_positions(aaa=2, bbb=2),
    )

    assert orders == [("AAA", "NYSE", -2)]
    volatility_state = portfolio_manager_with_db.data_store.get_last_event_payload(
        "volatility_weight_state"
    )
    assert volatility_state["symbols"]["AAA"]["effective_weight"] == pytest.approx(0.4)
    assert volatility_state["total_effective_weight"] == pytest.approx(0.9)

    trend_state = portfolio_manager_with_db.data_store.get_last_event_payload(
        "absolute_trend_state"
    )
    trend = trend_state["symbols"]["AAA"]
    assert trend["latest_close"] == pytest.approx(90.0)
    assert trend["moving_average"] == pytest.approx(100.0)
    assert trend["lookback_return"] == pytest.approx(-0.1)
    assert trend["state"] == "risk_off"
    assert trend["pre_trend_target"] == pytest.approx(0.4)
    assert trend["final_target"] == pytest.approx(0.06)
    assert trend["applied_multiplier"] == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_absolute_trend_zero_multiplier_can_exit_all_symbols(
    portfolio_manager_with_db, mocker
):
    portfolio_manager = portfolio_manager_with_db
    for symbol in ("AAA", "BBB"):
        symbol_config = portfolio_manager.config.portfolio.symbols[symbol]
        symbol_config.absolute_trend = _absolute_trend(
            lookback_days=3,
            risk_off_multiplier=0.0,
        )
        symbol_config.sell_only_min_threshold_percent_relative = 0.5
    portfolio_manager.config.strategies.regime_rebalance.ratio_gate = (
        _ratio_gate_config()
    )
    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_histories(
        portfolio_manager,
        mocker,
        {
            "AAA": [100.0, 100.0, 100.0, 90.0],
            "BBB": [100.0, 100.0, 100.0, 90.0],
        },
    )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    _, orders = await portfolio_manager.regime_engine.check_regime_rebalance_positions(
        _regime_account_summary(),
        _regime_stock_positions(aaa=2, bbb=2),
    )

    assert orders == [("AAA", "NYSE", -2), ("BBB", "NYSE", -2)]
    gate = portfolio_manager.data_store.get_last_event_payload("regime_rebalance_gate")
    assert gate["max_relative_drift"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_absolute_trend_disabled_is_noop(portfolio_manager, mocker):
    portfolio_manager.config.portfolio.symbols["AAA"].absolute_trend = _absolute_trend(
        enabled=False
    )
    history_cache = SimpleNamespace(get=mocker.AsyncMock())
    original = {"AAA": 0.4, "BBB": 0.6}

    weights, details = await portfolio_manager.regime_engine._apply_absolute_trend(
        original,
        portfolio_manager.config.portfolio.symbols,
        history_cache,
    )

    assert weights == original
    assert details == {}
    history_cache.get.assert_not_awaited()


@pytest.mark.parametrize(
    ("state_quantity", "live_quantity", "market_value", "expected_value"),
    [
        (1, 2, 240.0, 120.0),
        (2, 1, 120.0, 120.0),
        (1, float("nan"), 120.0, 0.0),
        (1, 2, float("nan"), 0.0),
    ],
)
def test_tail_hedge_market_value_counts_only_state_owned_quantity(
    state_quantity,
    live_quantity,
    market_value,
    expected_value,
):
    state_position = _option_position(
        "BBB",
        state_quantity,
        market_value=120.0,
        right="P",
        con_id=801,
        average_cost=50.0,
    )
    cohort = _tail_state(symbol="BBB", puts=[state_position]).open_cohorts[0]
    live_position = _option_position(
        "BBB",
        live_quantity,
        market_value=market_value,
        right="P",
        con_id=801,
        average_cost=50.0,
    )

    assert regime_engine_module.RegimeRebalanceEngine._tail_hedge_market_value(
        {"BBB": [live_position]},
        [cohort],
    ) == pytest.approx(expected_value)


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
    now = _naive_utc(2024, 1, 5, 12, 0, 0)
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
    now = _naive_utc(2024, 1, 5, 12, 0, 0)
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
    now = _naive_utc(2024, 1, 10, 12, 0, 0)
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
                orderRef="tg:other:AAA", time=_naive_utc(2024, 1, 5)
            ),
            contract=SimpleNamespace(symbol="AAA"),
            time=_naive_utc(2024, 1, 5),
        ),
        SimpleNamespace(
            execution=SimpleNamespace(
                orderRef="tg:regime-rebalance:CCC", time=_naive_utc(2024, 1, 6)
            ),
            contract=SimpleNamespace(symbol="CCC"),
            time=_naive_utc(2024, 1, 6),
        ),
        SimpleNamespace(
            execution=SimpleNamespace(
                orderRef="tg:regime-rebalance:BBB", time=_naive_utc(2024, 1, 7)
            ),
            contract=SimpleNamespace(symbol="BBB"),
            time=_naive_utc(2024, 1, 7),
        ),
    ]
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=fills)

    last_rebalance = (
        await portfolio_manager.regime_engine._get_last_regime_rebalance_time(
            ["AAA", "BBB"]
        )
    )

    assert last_rebalance == _naive_utc(2024, 1, 7)
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
                time=_naive_utc(2024, 1, 5, 12, 0, 0),
            ),
            contract=SimpleNamespace(symbol="AAA"),
            time=_naive_utc(2024, 1, 5, 12, 0, 0),
        ),
        SimpleNamespace(
            execution=SimpleNamespace(
                execId="2",
                acctNumber="TEST123",
                orderRef="tg:regime-rebalance:BBB",
                time=_naive_utc(2024, 1, 7, 12, 0, 0),
            ),
            contract=SimpleNamespace(symbol="BBB"),
            time=_naive_utc(2024, 1, 7, 12, 0, 0),
        ),
    ]
    portfolio_manager_with_db.data_store.record_executions(fills)
    portfolio_manager_with_db.ibkr.request_executions = mocker.AsyncMock(
        return_value=[]
    )
    _freeze_now(monkeypatch, _naive_utc(2024, 1, 10, 12, 0, 0))

    last_rebalance = (
        await portfolio_manager_with_db.regime_engine._get_last_regime_rebalance_time(
            ["AAA", "BBB"]
        )
    )

    assert last_rebalance == _naive_utc(2024, 1, 7, 12, 0, 0)


@pytest.mark.asyncio
async def test_regime_cooldown_uses_live_fill_when_persistence_drops_it(
    portfolio_manager_with_db, mocker, monkeypatch
):
    portfolio_manager = portfolio_manager_with_db
    _freeze_now(monkeypatch, _naive_utc(2024, 1, 10, 12))
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            execId="live-only",
            acctNumber="TEST123",
            orderRef="tg:regime-rebalance:AAA",
            time=_naive_utc(2024, 1, 9, 12),
        ),
        contract=SimpleNamespace(symbol="AAA"),
        time=_naive_utc(2024, 1, 9, 12),
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

    assert last_rebalance == _naive_utc(2024, 1, 9, 12)
    record_executions.assert_called_once_with([fill])


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_fails", [False, True])
async def test_regime_cooldown_uses_legacy_history_without_scoped_rows(
    portfolio_manager_with_db, mocker, monkeypatch, refresh_fails
):
    portfolio_manager = portfolio_manager_with_db
    _freeze_now(monkeypatch, _naive_utc(2024, 1, 10, 12))
    with portfolio_manager.data_store.session_scope() as session:
        session.add(
            ExecutionRecord(
                run_id=portfolio_manager.data_store.run_id,
                exec_id="legacy",
                account=None,
                order_ref="tg:regime-rebalance:AAA",
                symbol="AAA",
                execution_time=_naive_utc(2024, 1, 9, 12),
            )
        )
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(
        side_effect=ConnectionError("offline") if refresh_fails else None,
        return_value=[],
    )

    last_rebalance = (
        await portfolio_manager.regime_engine._get_last_regime_rebalance_time(["AAA"])
    )

    assert last_rebalance == _naive_utc(2024, 1, 9, 12)


@pytest.mark.asyncio
async def test_regime_cooldown_fails_closed_when_history_is_unavailable(
    portfolio_manager_with_db, mocker, monkeypatch
):
    fixed = _naive_utc(2024, 1, 10, 12)
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
    now = _naive_utc(2024, 1, 5, 12, 0, 0)
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
        "flow_rebalance_capacity": 400.0,
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
    now = _naive_utc(2024, 1, 5, 12, 0, 0)
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

    start_date = _naive_utc(2024, 1, 2)
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
        annual_budget=0.005,
        harvest_trigger_weight=0.05,
        harvest_target_weight=0.03,
        harvest_decision=SimpleNamespace(enabled=False),
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


def _enable_external_tail_harvest(
    portfolio_manager,
    provider: _FixedTailHarvestProvider,
) -> None:
    portfolio_manager.config.strategies.tail_hedge.harvest_decision = SimpleNamespace(
        enabled=True,
        provider="tail-fixture",
        on_error="baseline",
        max_signal_age_sessions=0,
        market_data=SimpleNamespace(
            lookback_days=3,
            include_strategy_symbols=True,
            symbols={},
        ),
    )
    portfolio_manager.config.strategies.tail_hedge.targets = [
        SimpleNamespace(
            symbol="BBB",
            budget_weight=1.0,
            entries_per_year=6,
            entry_gate="vix",
            entry_vix_max=20.0,
            target_dte=180,
            min_dte=120,
            max_dte=240,
            exit_dte=30,
            minimum_open_interest=50,
            minimum_bid=0.01,
            max_bid_ask_ratio=0.5,
            max_premium_ratio=0.05,
            catastrophe_drawdowns=[0.4, 0.5, 0.6],
        )
    ]
    portfolio_manager.external_decisions.replace("tail-fixture", provider)


def _mock_tail_harvest_history(portfolio_manager, mocker) -> list[date]:
    sessions = [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2)]
    mocker.patch.object(
        portfolio_manager.regime_engine,
        "_get_regime_aligned_closes",
        new=mocker.AsyncMock(return_value=(sessions, {"BBB": [100.0, 92.0, 85.0]})),
    )
    return sessions


def _tail_harvest_regime_summary() -> list[dict[str, object]]:
    return [
        {
            "symbol": "BBB",
            "current_shares": 4,
            "current_value": 340.0,
            "current_weight": 0.17,
            "target_weight": 0.50,
            "target_value": 1_000.0,
            "target_shares": 11,
            "volatility_weight": {"effective_weight": 0.50},
            "target_weight_policy": None,
            "absolute_trend": {"risk_on": False},
        }
    ]


def _prepare_external_tail_harvest(
    portfolio_manager,
    mocker,
    *,
    provider: _FixedTailHarvestProvider,
    con_id: int,
    unrealized_pnl: float | None = None,
) -> tuple[TailHedgeState, list[date]]:
    _configure_tail_harvest(portfolio_manager, "BBB")
    _enable_external_tail_harvest(portfolio_manager, provider)
    sessions = _mock_tail_harvest_history(portfolio_manager, mocker)
    tail_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=con_id,
        average_cost=50.0,
        unrealized_pnl=unrealized_pnl,
    )
    tail_put.contract.multiplier = "100"
    payload = _tail_state(symbol="BBB", puts=[tail_put])
    _save_tail_state(portfolio_manager, payload)
    _set_live_tail_positions(portfolio_manager, [tail_put])
    return payload, sessions


@pytest.mark.parametrize(
    ("on_error", "expected_harvest", "expected_status"),
    [("baseline", True, "baseline"), ("skip", False, "skipped")],
)
def test_external_tail_harvest_nonfatal_failure_policy(
    portfolio_manager,
    on_error: str,
    expected_harvest: bool,
    expected_status: str,
) -> None:
    harvest, detail = portfolio_manager.regime_engine._tail_harvest_decision_fallback(
        policy=SimpleNamespace(on_error=on_error, provider="tail-fixture"),
        error="provider unavailable",
    )

    assert harvest is expected_harvest
    assert detail["status"] == expected_status
    assert detail["error"] == "provider unavailable"


def test_external_tail_harvest_abort_failure_policy(portfolio_manager) -> None:
    with pytest.raises(RuntimeError, match="provider unavailable"):
        portfolio_manager.regime_engine._tail_harvest_decision_fallback(
            policy=SimpleNamespace(on_error="abort", provider="tail-fixture"),
            error="provider unavailable",
        )


@pytest.mark.asyncio
async def test_external_tail_harvest_policy_can_veto_eligible_harvest(
    portfolio_manager_with_db,
    mocker,
) -> None:
    portfolio_manager = portfolio_manager_with_db
    provider = _FixedTailHarvestProvider(False)
    payload, sessions = _prepare_external_tail_harvest(
        portfolio_manager,
        mocker,
        provider=provider,
        con_id=799,
        unrealized_pnl=70.0,
    )
    _set_tail_quotes(portfolio_manager, mocker, {799: 1.20})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 7)],
        net_liquidation=2_000.0,
        market_prices={"BBB": 85.0},
        regime_summary=_tail_harvest_regime_summary(),
        hard_underweight_symbols={"BBB"},
        cohorts=payload.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 7)]
    assert portfolio_manager.orders.records() == []
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.decision_type == "tail_hedge_harvest"
    assert request.input["market_data"] == {
        "source": "ibkr",
        "timeframe": "1 day",
        "what_to_show": "TRADES",
        "regular_trading_hours_only": True,
        "sessions": [session.isoformat() for session in sessions],
        "closes": {"BBB": [100.0, 92.0, 85.0]},
        "primary_exchanges": {"BBB": "NYSE"},
    }
    underlying = request.input["underlyings"]["BBB"]
    assert underlying["current_shares"] == pytest.approx(4.0)
    assert underlying["current_weight"] == pytest.approx(0.17)
    assert underlying["approved_buy_shares"] == 7
    assert underlying["broker_position"] == {
        "shares": 0.0,
        "market_value": 0,
        "average_cost_per_share": None,
        "unrealized_pnl": 0,
        "realized_pnl": 0,
    }
    assert underlying["target_modifiers"]["absolute_trend"] == {"risk_on": False}
    hedge = request.input["hedge_positions"][0]
    assert hedge["state_owned_quantity"] == 1
    assert hedge["live_position_quantity"] == pytest.approx(1.0)
    assert hedge["state_owned_unrealized_pnl"] == pytest.approx(70.0)
    assert hedge["quoted_limit_price"] == pytest.approx(1.20)
    assert hedge["host_candidate"] is True
    assert hedge["candidate"]["net_proceeds_per_contract"] == pytest.approx(120.0)
    assert request.input["host_constraints"] == {
        "baseline_band_triggered": True,
        "requires_approved_same_symbol_hard_underweight_buy": True,
        "state_owned_active_profitable_puts_only": True,
        "host_selects_contracts_quantities_and_limit_prices": True,
    }
    assert request.input["opportunity"]["sale_budget"] == pytest.approx(63.6)
    assert request.input["opportunity"]["planned_sales"] == [
        {
            "entry_id": "BBB-tail-799",
            "symbol": "BBB",
            "con_id": 799,
            "expiration": "20261120",
            "quantity": 1,
            "limit_price": 1.2,
            "estimated_gross_proceeds": 120.0,
            "estimated_fees": 0.0,
            "estimated_net_proceeds": 120.0,
        }
    ]
    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None
    assert store.load().open_cohorts[0].pending_recovery_quantity is None


@pytest.mark.asyncio
async def test_external_tail_harvest_approval_is_revalidated_before_order(
    portfolio_manager_with_db,
    mocker,
) -> None:
    portfolio_manager = portfolio_manager_with_db
    provider = _FixedTailHarvestProvider(True)
    payload, _ = _prepare_external_tail_harvest(
        portfolio_manager,
        mocker,
        provider=provider,
        con_id=800,
    )
    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(
        side_effect=[_option_ticker(1.20), _option_ticker(0.40)]
    )

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 7)],
        net_liquidation=2_000.0,
        market_prices={"BBB": 85.0},
        regime_summary=_tail_harvest_regime_summary(),
        hard_underweight_symbols={"BBB"},
        cohorts=payload.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 7)]
    assert len(provider.requests) == 1
    assert portfolio_manager.ibkr.get_ticker_for_contract.await_count == 2
    assert portfolio_manager.orders.records() == []
    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None
    assert store.load().open_cohorts[0].pending_recovery_quantity is None


@pytest.mark.asyncio
@pytest.mark.parametrize("on_error", ["baseline", "skip", "abort"])
async def test_tail_harvest_approval_expiring_during_requote_uses_failure_policy(
    portfolio_manager_with_db: Any, mocker: Any, on_error: str
) -> None:
    manager = portfolio_manager_with_db
    engine = manager.regime_engine
    now = datetime(2026, 9, 3, 14, 30, tzinfo=UTC)
    provider = _FixedTailHarvestProvider(True, expires_at=now + timedelta(seconds=5))
    payload, _ = _prepare_external_tail_harvest(
        manager,
        mocker,
        provider=provider,
        con_id=802,
    )
    manager.config.strategies.tail_hedge.harvest_decision.on_error = on_error
    engine._now = mocker.Mock(return_value=now)
    quotes = 0

    async def quote(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        nonlocal quotes
        quotes += 1
        if quotes == 2:
            engine._now.return_value = now + timedelta(seconds=5)
        return _option_ticker(1.20)

    manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(side_effect=quote)
    kwargs = {
        "orders": [("BBB", "NYSE", 7)],
        "net_liquidation": 2_000.0,
        "market_prices": {"BBB": 85.0},
        "regime_summary": _tail_harvest_regime_summary(),
        "hard_underweight_symbols": {"BBB"},
        "cohorts": payload.open_cohorts,
    }
    if on_error == "abort":
        with pytest.raises(RuntimeError, match="expired"):
            await engine._apply_tail_harvest(**kwargs)
    else:
        await engine._apply_tail_harvest(**kwargs)

    assert len(provider.requests) == 1
    assert quotes == 2
    assert bool(manager.orders.records()) is (on_error == "baseline")
    state = engine._tail_state_store.load()
    assert (state.open_cohorts[0].pending_recovery_quantity is not None) is (
        on_error == "baseline"
    )


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
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=payload.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    queued_order = portfolio_manager.orders.records()[0][1]
    assert getattr(queued_order, TAIL_HEDGE_MIN_LIMIT_PRICE_ATTR) == pytest.approx(0.95)
    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None
    state = store.load()
    assert state.open_cohorts[0].pending_recovery_quantity == 1
    assert state.open_cohorts[0].pending_recovery_per_contract == 95.0
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
    assert telemetry["minimum_limit_price"] == 0.95
    assert telemetry["minimum_net_proceeds_per_contract"] == 95.0
    assert telemetry["rebalance_shares"] == 1
    assert telemetry["net_liquidation"] == 2_000.0
    assert telemetry["regime_rebalance_base"] == 1_880
    assert telemetry["regime_weight_base"] == "net_liq_ex_options"
    assert telemetry["excluded_option_value"] == 120.0
    assert telemetry["sleeve_value"] == 120.0
    assert telemetry["sleeve_weight"] == pytest.approx(120.0 / 1_880.0)
    assert telemetry["sale_budget"] == pytest.approx(63.6)
    assert telemetry["harvest_trigger_weight"] == 0.05
    assert telemetry["harvest_target_weight"] == 0.03


@pytest.mark.asyncio
async def test_harvest_band_does_not_trigger_at_upper_bound(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    tail_put = _option_position(
        "BBB",
        1,
        market_value=100.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    tail_put.contract.multiplier = "100"
    state = _tail_state(symbol="BBB", puts=[tail_put])
    _set_live_tail_positions(portfolio_manager, [tail_put])
    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock()

    await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        net_liquidation=2_100.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=state.open_cohorts,
    )

    assert portfolio_manager.orders.records() == []
    portfolio_manager.ibkr.get_ticker_for_contract.assert_not_awaited()


@pytest.mark.asyncio
async def test_harvest_band_sizes_contracts_by_market_value_not_net_cash(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    portfolio_manager.config.runtime.orders.estimated_fee_per_contract = 1.0
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
    state = _tail_state(symbol="BBB", puts=[tail_put])
    _set_live_tail_positions(portfolio_manager, [tail_put])
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        net_liquidation=4_240.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=state.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    queued = portfolio_manager.orders.records()
    assert len(queued) == 1
    assert int(queued[0][1].totalQuantity) == 1


@pytest.mark.asyncio
async def test_harvest_reuses_regime_option_box_and_cash_fund_base(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    tail_put = _option_position(
        "BBB",
        1,
        market_value=4_700.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=1_000.0,
    )
    tail_put.contract.multiplier = "100"
    state = _tail_state(symbol="BBB", puts=[tail_put])
    _set_live_tail_positions(portfolio_manager, [tail_put])
    manual_regime_option = _option_position(
        "AAA",
        1,
        market_value=5_000.0,
        con_id=802,
    )
    cash_fund = _stock_position("SHV", 100, market_value=15_000.0)
    box_legs = [
        _option_position(
            "SPX",
            -1,
            market_value=-1_200_000.0,
            strike=5_000.0,
            right="C",
            expiry="20260716",
            con_id=901,
        ),
        _option_position(
            "SPX",
            1,
            market_value=2_000.0,
            strike=5_000.0,
            right="P",
            expiry="20260716",
            con_id=902,
        ),
        _option_position(
            "SPX",
            1,
            market_value=700_000.0,
            strike=5_100.0,
            right="C",
            expiry="20260716",
            con_id=903,
        ),
        _option_position(
            "SPX",
            -1,
            market_value=-2_000.0,
            strike=5_100.0,
            right="P",
            expiry="20260716",
            con_id=904,
        ),
    ]
    portfolio_manager.ibkr.ib.portfolio.return_value = [
        tail_put,
        manual_regime_option,
        cash_fund,
        *box_legs,
    ]
    _set_tail_quotes(portfolio_manager, mocker, {801: 47.0})

    await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        net_liquidation=100_000.0,
        market_prices={"AAA": 100.0, "BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=state.open_cohorts,
    )

    assert len(portfolio_manager.orders.records()) == 1
    telemetry = portfolio_manager.data_store.get_last_event_payload(
        TAIL_HEDGE_HARVEST_EVENT,
        symbol="BBB",
    )
    assert telemetry is not None
    assert telemetry["net_liquidation"] == 100_000.0
    assert telemetry["excluded_option_value"] == 9_700.0
    assert telemetry["regime_rebalance_base"] == 90_300
    assert telemetry["sleeve_weight"] == pytest.approx(4_700.0 / 90_300.0)


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
        enqueued_at=datetime.now().astimezone().replace(tzinfo=None)
        - timedelta(minutes=10),
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
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=store.load().open_cohorts,
    )

    assert first_orders == [("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []
    telemetry = portfolio_manager.data_store.get_last_event_payload(
        TAIL_HEDGE_HARVEST_EVENT,
        symbol="BBB",
    )
    assert telemetry is not None
    assert telemetry["outcome"] == "harvest_blocked"
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
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=store.load().open_cohorts,
    )

    assert next_orders == [("BBB", "NYSE", 1)]
    assert [
        contract.conId
        for contract, _order, _intent_id in portfolio_manager.orders.records()
    ] == [802]


@pytest.mark.asyncio
async def test_pending_recovery_serializes_portfolio_wide_harvests(
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
        expiry="20261218",
        con_id=802,
        average_cost=50.0,
    )
    aaa_put.contract.multiplier = bbb_put.contract.multiplier = "100"
    _save_tail_state(
        portfolio_manager,
        _tail_state(symbol="AAA", puts=[aaa_put]),
        _tail_state(symbol="BBB", puts=[bbb_put]),
    )
    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None
    state = store.load()
    bbb_cohort = state.find_open_by_con_id(802)
    assert bbb_cohort is not None
    bbb_cohort.begin_recovery(
        quantity=1,
        proceeds_per_contract=120.0,
        enqueued_at=datetime.now().astimezone().replace(tzinfo=None)
        - timedelta(minutes=10),
    )
    store.save(state)
    portfolio_manager.ibkr.ib.portfolio.return_value = [aaa_put, bbb_put]
    portfolio_manager.ibkr.ib.openTrades.return_value = []
    portfolio_manager.ibkr.ib.accountValues.return_value = []
    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock()

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("AAA", "NYSE", 1)],
        net_liquidation=2_000.0,
        market_prices={"AAA": 100.0},
        regime_summary=[{"symbol": "AAA"}],
        hard_underweight_symbols={"AAA"},
        cohorts=store.load().open_cohorts,
    )

    assert orders == [("AAA", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []
    portfolio_manager.ibkr.get_ticker_for_contract.assert_not_awaited()
    telemetry = portfolio_manager.data_store.get_last_event_payload(
        TAIL_HEDGE_HARVEST_EVENT,
        symbol="AAA",
    )
    assert telemetry is not None
    assert telemetry["outcome"] == "harvest_blocked"
    assert telemetry["reason"] == "pending_recovery"
    assert telemetry["blocking_symbols"] == ["BBB"]


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
        net_liquidation=1_000.0,
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
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=payload.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    await portfolio_manager.post_engine.tail_hedge_engine.manage(
        {"BBB": [live_put]},
        net_liquidation=200.0,
    )

    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None
    cohort = store.load().open_cohorts[0]
    assert cohort.quantity == 2
    assert cohort.recovered_cost == 0.0
    assert cohort.pending_recovery_quantity == 2


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
        net_liquidation=2_000.0,
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
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=payload.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []


@pytest.mark.asyncio
async def test_unrelated_working_stock_order_does_not_block_tail_harvest(
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
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert len(portfolio_manager.orders.records()) == 1


@pytest.mark.asyncio
async def test_harvest_rereads_replacement_ib_async_cache_objects(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
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

    live_ib = IB()
    live_ib.wrapper.accountValues[("TEST123", "NetLiquidation", "BASE", "")] = (
        AccountValue("TEST123", "NetLiquidation", "2000", "BASE", "")
    )
    live_ib.wrapper.portfolio["TEST123"][802] = live_put
    account_values = mocker.spy(live_ib, "accountValues")
    account_summary_async = mocker.spy(live_ib, "accountSummaryAsync")
    request_positions = mocker.spy(live_ib, "reqPositionsAsync")
    portfolio_manager.ibkr.ib = live_ib
    _set_tail_quotes(portfolio_manager, mocker, {802: 1.20})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 2)],
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=live_state.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 2)]
    assert [
        contract.conId
        for contract, _order, _intent_id in portfolio_manager.orders.records()
    ] == [802]
    assert account_values.call_args_list == [mocker.call("TEST123")] * 2
    account_summary_async.assert_not_called()
    request_positions.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_working_sale", [False, True])
async def test_harvest_revalidates_earlier_candidates_after_all_quotes(
    portfolio_manager_with_db,
    mocker,
    late_working_sale,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "AAA", "BBB")
    stale_put = _option_position(
        "AAA",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    live_put = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261218",
        con_id=802,
        average_cost=50.0,
    )
    stale_put.contract.multiplier = live_put.contract.multiplier = "100"
    _set_live_tail_positions(portfolio_manager, [stale_put, live_put])

    async def quote(contract, **_kwargs):
        if contract.conId == 802:
            portfolio_manager.ibkr.ib.portfolio.return_value = [live_put]
            if late_working_sale:
                portfolio_manager.ibkr.ib.openTrades.return_value = [
                    SimpleNamespace(
                        contract=stale_put.contract,
                        order=SimpleNamespace(
                            account="TEST123",
                            action="SELL",
                            orderRef=None,
                        ),
                        isDone=lambda: False,
                    )
                ]
        return _option_ticker(1.20)

    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(side_effect=quote)
    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("AAA", "NYSE", 1), ("BBB", "NYSE", 1)],
        net_liquidation=2_000.0,
        market_prices={"AAA": 100.0, "BBB": 100.0},
        regime_summary=[{"symbol": "AAA"}, {"symbol": "BBB"}],
        hard_underweight_symbols={"AAA", "BBB"},
        cohorts=store.load().open_cohorts,
    )

    assert orders == [("AAA", "NYSE", 1), ("BBB", "NYSE", 1)]
    queued_con_ids = [
        contract.conId
        for contract, _order, _intent_id in portfolio_manager.orders.records()
    ]
    state = store.load()
    stale_cohort = state.find_open_by_con_id(801)
    live_cohort = state.find_open_by_con_id(802)
    assert stale_cohort is not None and not stale_cohort.has_pending_recovery
    assert live_cohort is not None
    if late_working_sale:
        assert queued_con_ids == []
        assert not live_cohort.has_pending_recovery
        telemetry = portfolio_manager.data_store.get_last_event_payload(
            TAIL_HEDGE_HARVEST_EVENT,
            symbol="BBB",
        )
        assert telemetry is not None
        assert telemetry["outcome"] == "harvest_blocked"
        assert telemetry["reason"] == "working_tail_sale"
        assert telemetry["blocking_symbols"] == ["AAA"]
    else:
        assert queued_con_ids == [802]
        assert live_cohort.has_pending_recovery


@pytest.mark.asyncio
async def test_harvest_rechecks_band_after_quotes_before_saving_intent(
    portfolio_manager_with_db,
    mocker,
):
    portfolio_manager = portfolio_manager_with_db
    _configure_tail_harvest(portfolio_manager, "BBB")
    before_quote = _option_position(
        "BBB",
        1,
        market_value=120.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    after_quote = _option_position(
        "BBB",
        1,
        market_value=80.0,
        right="P",
        expiry="20261120",
        con_id=801,
        average_cost=50.0,
    )
    before_quote.contract.multiplier = after_quote.contract.multiplier = "100"
    state = _tail_state(symbol="BBB", puts=[before_quote])
    _set_live_tail_positions(portfolio_manager, [before_quote])

    async def quote(_contract, **_kwargs):
        portfolio_manager.ibkr.ib.portfolio.return_value = [after_quote]
        return _option_ticker(1.20)

    portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(side_effect=quote)

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=state.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []
    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None
    assert not store.load().open_cohorts[0].has_pending_recovery


@pytest.mark.asyncio
async def test_harvest_rechecks_band_with_fresh_quote_value(
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
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.00})
    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        net_liquidation=2_100.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=store.load().open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []
    cohort = store.load().find_open_by_con_id(801)
    assert cohort is not None and not cohort.has_pending_recovery


@pytest.mark.asyncio
async def test_harvest_rechecks_net_liquidation_after_quotes(
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
    state = _tail_state(symbol="BBB", puts=[tail_put])
    _set_live_tail_positions(portfolio_manager, [tail_put])
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20})
    portfolio_manager.ibkr.cached_net_liquidation = mocker.Mock(
        side_effect=[2_000.0, 3_000.0]
    )

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=state.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert portfolio_manager.orders.records() == []
    assert portfolio_manager.ibkr.cached_net_liquidation.call_args_list == [
        mocker.call("TEST123"),
        mocker.call("TEST123"),
    ]
    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None
    assert not store.load().open_cohorts[0].has_pending_recovery


@pytest.mark.asyncio
async def test_harvest_uses_current_net_liquidation_for_initial_band_gate(
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
    state = _tail_state(symbol="BBB", puts=[tail_put])
    _set_live_tail_positions(portfolio_manager, [tail_put])
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20})
    portfolio_manager.ibkr.cached_net_liquidation = mocker.Mock(return_value=2_000.0)

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        net_liquidation=3_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=state.open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert len(portfolio_manager.orders.records()) == 1
    assert portfolio_manager.ibkr.get_ticker_for_contract.await_count == 1
    assert portfolio_manager.ibkr.cached_net_liquidation.call_count == 2


@pytest.mark.asyncio
async def test_harvest_sells_earliest_cohorts_toward_band_target(
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
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(
            symbol="BBB",
            puts=[latest_put, later_put, short_put],
        ).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 3)]
    queued = portfolio_manager.orders.records()
    assert [contract.conId for contract, _order, _intent_id in queued] == [
        801,
        802,
        803,
    ]
    assert [int(order.totalQuantity) for _contract, order, _intent_id in queued] == [
        1,
        1,
        1,
    ]
    assert all(
        order.orderRef.startswith(f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:BBB:")
        for _contract, order, _intent_id in queued
    )


@pytest.mark.asyncio
async def test_harvest_keeps_full_rebalance_when_tranche_covers_only_part(
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
    _set_live_tail_positions(portfolio_manager, [tail_put])
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.0})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 20)],
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 20)]
    assert [
        int(order.totalQuantity)
        for _contract, order, _intent_id in portfolio_manager.orders.records()
    ] == [6]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cash", "still_hard", "expected_sale"),
    [
        (300.0, True, [801, 802]),
        (-500_000.0, True, [801, 802]),
        (300.0, False, []),
    ],
)
async def test_harvest_ignores_cash_and_requires_hard_underweight(
    portfolio_manager_with_db,
    mocker,
    cash,
    still_hard,
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
    _set_live_tail_positions(portfolio_manager, [first_put, next_put], cash=cash)
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.2, 802: 1.2})

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 3)],
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"} if still_hard else set(),
        cohorts=cohorts,
    )

    assert orders == [("BBB", "NYSE", 3)]
    assert [
        contract.conId
        for contract, _order, _intent_id in portfolio_manager.orders.records()
    ] == expected_sale


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "order_ref"),
    [
        ("broker", f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:BBB:801"),
        ("broker", TAIL_HEDGE_CLOSE_ORDER_REF),
        ("broker", None),
        ("local", TAIL_HEDGE_CLOSE_ORDER_REF),
    ],
)
async def test_working_tail_sale_blocks_duplicate_without_changing_rebalance(
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
    _set_live_tail_positions(portfolio_manager, [tail_put])
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
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 2)]
    assert len(portfolio_manager.orders.records()) == (1 if source == "local" else 0)
    portfolio_manager.ibkr.get_ticker_for_contract.assert_not_awaited()


@pytest.mark.asyncio
async def test_nan_live_cost_uses_persisted_entry_limit_basis(
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
        average_cost=float("nan"),
    )
    tail_put.contract.multiplier = "100"
    _set_live_tail_positions(portfolio_manager, [tail_put])
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20})
    store = portfolio_manager.regime_engine._tail_state_store
    assert store is not None

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("BBB", "NYSE", 1)],
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=store.load().open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert len(portfolio_manager.orders.records()) == 1


@pytest.mark.asyncio
async def test_harvest_is_not_gated_by_one_share_of_rebalance_value(
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
        net_liquidation=1_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[tail_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert len(portfolio_manager.orders.records()) == 1


@pytest.mark.asyncio
async def test_harvest_combines_earliest_profitable_cohorts_toward_target(
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
        net_liquidation=2_000.0,
        market_prices={"BBB": 100.0},
        regime_summary=[{"symbol": "BBB"}],
        hard_underweight_symbols={"BBB"},
        cohorts=_tail_state(symbol="BBB", puts=[shortest_put, later_put]).open_cohorts,
    )

    assert orders == [("BBB", "NYSE", 1)]
    assert [
        contract.conId
        for contract, _order, _intent_id in portfolio_manager.orders.records()
    ] == [801, 802]


@pytest.mark.asyncio
async def test_harvest_combines_eligible_symbols_for_portfolio_band(
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
    _set_live_tail_positions(portfolio_manager, [aaa_put, bbb_put])
    _set_tail_quotes(portfolio_manager, mocker, {801: 1.20, 802: 1.20})
    cohorts = [
        *_tail_state(symbol="AAA", puts=[aaa_put]).open_cohorts,
        *_tail_state(symbol="BBB", puts=[bbb_put]).open_cohorts,
    ]

    orders = await portfolio_manager.regime_engine._apply_tail_harvest(
        orders=[("AAA", "NYSE", 1), ("BBB", "NYSE", 1)],
        net_liquidation=2_000.0,
        market_prices={"AAA": 100.0, "BBB": 100.0},
        regime_summary=[{"symbol": "AAA"}, {"symbol": "BBB"}],
        hard_underweight_symbols={"AAA", "BBB"},
        cohorts=cohorts,
    )

    assert orders == [("AAA", "NYSE", 1), ("BBB", "NYSE", 1)]
    assert [
        contract.symbol
        for contract, _order, _intent_id in portfolio_manager.orders.records()
    ] == ["AAA", "BBB"]
    assert portfolio_manager.ibkr.get_ticker_for_contract.await_count == 2


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
        net_liquidation=2_000.0,
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
