from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from ib_async import IB, Stock

import thetagang.portfolio_manager as pm_module
from thetagang.config import RatioGateConfig, RegimeRebalanceConfig, normalize_config
from thetagang.db import DataStore
from thetagang.portfolio_manager import PortfolioManager


@pytest.fixture
def mock_ib(mocker):
    mock = mocker.Mock(spec=IB)
    mock.orderStatusEvent = mocker.Mock()
    mock.orderStatusEvent.__iadd__ = mocker.Mock(return_value=None)
    return mock


@pytest.fixture
def portfolio_manager(mock_ib, mocker):
    config = mocker.Mock()
    config.account = mocker.Mock()
    config.account.number = "TEST123"
    config.account.margin_usage = 1.0
    config.ib_async = mocker.Mock()
    config.ib_async.api_response_wait_time = 1
    config.orders = mocker.Mock()
    config.orders.exchange = "SMART"
    config.orders.algo = mocker.Mock()
    config.orders.algo.strategy = "Adaptive"
    config.orders.algo.params = []
    config.exchange_hours = mocker.Mock()
    config.exchange_hours.exchange = "XNYS"
    config.trading_is_allowed = mocker.Mock(return_value=True)

    config.symbols = {
        "AAA": SimpleNamespace(weight=0.5, primary_exchange="NYSE"),
        "BBB": SimpleNamespace(weight=0.5, primary_exchange="NYSE"),
    }
    config.regime_rebalance = SimpleNamespace(
        enabled=True,
        symbols=["AAA", "BBB"],
        lookback_days=3,
        soft_band=0.10,
        hard_band=0.80,
        hard_band_rebalance_fraction=1.0,
        cooldown_days=2,
        choppiness_min=0.1,
        efficiency_max=0.9,
        flow_trade_min=2000.0,
        flow_trade_stop=1000.0,
        flow_imbalance_tau=0.7,
        deficit_rail_start=5000.0,
        deficit_rail_stop=2500.0,
        eps=1e-8,
        order_history_lookback_days=30,
        shares_only=False,
    )

    completion_future = mocker.Mock()
    return PortfolioManager(config, mock_ib, completion_future, dry_run=False)


@pytest.fixture
def portfolio_manager_with_db(mock_ib, mocker, tmp_path):
    config = mocker.Mock()
    config.account = mocker.Mock()
    config.account.number = "TEST123"
    config.account.margin_usage = 1.0
    config.ib_async = mocker.Mock()
    config.ib_async.api_response_wait_time = 1
    config.orders = mocker.Mock()
    config.orders.exchange = "SMART"
    config.orders.algo = mocker.Mock()
    config.orders.algo.strategy = "Adaptive"
    config.orders.algo.params = []
    config.exchange_hours = mocker.Mock()
    config.exchange_hours.exchange = "XNYS"
    config.trading_is_allowed = mocker.Mock(return_value=True)

    config.symbols = {
        "AAA": SimpleNamespace(weight=0.5, primary_exchange="NYSE"),
        "BBB": SimpleNamespace(weight=0.5, primary_exchange="NYSE"),
    }
    config.regime_rebalance = SimpleNamespace(
        enabled=True,
        symbols=["AAA", "BBB"],
        lookback_days=3,
        soft_band=0.10,
        hard_band=0.80,
        hard_band_rebalance_fraction=1.0,
        cooldown_days=2,
        choppiness_min=0.1,
        efficiency_max=0.9,
        flow_trade_min=2000.0,
        flow_trade_stop=1000.0,
        flow_imbalance_tau=0.7,
        deficit_rail_start=5000.0,
        deficit_rail_stop=2500.0,
        eps=1e-8,
        order_history_lookback_days=30,
        shares_only=False,
    )

    data_store = DataStore(
        f"sqlite:///{tmp_path / 'state.db'}",
        str(tmp_path / "thetagang.toml"),
        dry_run=False,
        config_text="test",
    )

    completion_future = mocker.Mock()
    return PortfolioManager(
        config, mock_ib, completion_future, dry_run=False, data_store=data_store
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
    start_date = datetime(2024, 1, 2)
    bars = [
        SimpleNamespace(date=start_date + timedelta(days=offset), close=close)
        for offset, close in enumerate(closes)
    ]

    async def _get_history(*_args, **_kwargs):
        return bars

    portfolio_manager.ibkr.request_historical_data = mocker.AsyncMock(
        side_effect=_get_history
    )
    return bars


def _mock_regime_tickers(portfolio_manager, mocker, aaa_price=100.0, bbb_price=100.0):
    aaa_ticker = mocker.Mock()
    aaa_ticker.marketPrice.return_value = aaa_price
    bbb_ticker = mocker.Mock()
    bbb_ticker.marketPrice.return_value = bbb_price
    tickers = {"AAA": aaa_ticker, "BBB": bbb_ticker}

    async def _get_ticker(symbol, _primary_exchange):
        return tickers[symbol]

    portfolio_manager.ibkr.get_ticker_for_stock = mocker.AsyncMock(
        side_effect=_get_ticker
    )


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
async def test_regime_rebalance_respects_regime_gate(portfolio_manager, mocker):
    portfolio_manager.config.regime_rebalance.choppiness_min = 10.0

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
    portfolio_manager_with_db.config.regime_rebalance.ratio_gate = SimpleNamespace(
        enabled=False,
        anchor="BBB",
        drift_max=1.25,
        var_min=0.0,
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
    portfolio_manager.config.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.regime_rebalance.ratio_gate = SimpleNamespace(
        enabled=True,
        anchor="BBB",
        drift_max=1.25,
        var_min=0.0,
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
    portfolio_manager.config.regime_rebalance.soft_band = 0.30
    portfolio_manager.config.regime_rebalance.hard_band = 0.10
    portfolio_manager.config.regime_rebalance.choppiness_min = 10.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 0.01
    portfolio_manager.config.regime_rebalance.ratio_gate = SimpleNamespace(
        enabled=True,
        anchor="BBB",
        drift_max=1.25,
        var_min=0.0,
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

    with pytest.raises(ValueError, match="full lookback history"):
        await portfolio_manager.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )


@pytest.mark.asyncio
async def test_regime_rebalance_band_thresholds(portfolio_manager, mocker):
    portfolio_manager.config.regime_rebalance.soft_band = 0.10
    portfolio_manager.config.regime_rebalance.hard_band = 0.50
    portfolio_manager.config.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 1.0

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

    portfolio_manager.config.regime_rebalance.soft_band = 0.30
    portfolio_manager.config.regime_rebalance.hard_band = 0.10
    portfolio_manager.config.regime_rebalance.cooldown_days = 10
    portfolio_manager.config.regime_rebalance.choppiness_min = 10.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 0.01

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
    portfolio_manager.config.regime_rebalance.soft_band = 0.30
    portfolio_manager.config.regime_rebalance.hard_band = 0.10
    portfolio_manager.config.regime_rebalance.hard_band_rebalance_fraction = 0.5
    portfolio_manager.config.regime_rebalance.choppiness_min = 10.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 0.01

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
    portfolio_manager.config.regime_rebalance.soft_band = 0.10
    portfolio_manager.config.regime_rebalance.hard_band = 0.80
    portfolio_manager.config.regime_rebalance.choppiness_min = 10.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 0.01

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
async def test_regime_rebalance_flow_trades_ignore_regime_gate(
    portfolio_manager, mocker
):
    portfolio_manager.config.regime_rebalance.soft_band = 0.50
    portfolio_manager.config.regime_rebalance.hard_band = 0.80
    portfolio_manager.config.regime_rebalance.choppiness_min = 10.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 0.01
    portfolio_manager.config.regime_rebalance.flow_trade_min = 200.0
    portfolio_manager.config.regime_rebalance.flow_trade_stop = 100.0

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
    portfolio_manager.config.regime_rebalance.shares_only = True
    assert portfolio_manager.options_trading_enabled() is False


def test_regime_rebalance_config_rejects_inverted_bands():
    with pytest.raises(ValueError, match="hard_band"):
        RegimeRebalanceConfig(soft_band=0.5, hard_band=0.25)


def test_regime_rebalance_config_rejects_flow_hysteresis_inversion():
    with pytest.raises(ValueError, match="flow_trade_min"):
        RegimeRebalanceConfig(flow_trade_min=100.0, flow_trade_stop=200.0)


def test_regime_rebalance_config_rejects_deficit_hysteresis_inversion():
    with pytest.raises(ValueError, match="deficit_rail_start"):
        RegimeRebalanceConfig(deficit_rail_start=100.0, deficit_rail_stop=200.0)


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

    assert orders == [("BBB", "NYSE", 1)]


@pytest.mark.asyncio
async def test_regime_rebalance_cash_added_triggers_buys(portfolio_manager, mocker):
    portfolio_manager.config.regime_rebalance.soft_band = 0.5
    portfolio_manager.config.regime_rebalance.hard_band = 0.8
    portfolio_manager.config.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.regime_rebalance.flow_trade_min = 200.0
    portfolio_manager.config.regime_rebalance.flow_trade_stop = 100.0

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


@pytest.mark.asyncio
async def test_regime_rebalance_cash_withdrawn_triggers_sells(
    portfolio_manager, mocker
):
    portfolio_manager.config.regime_rebalance.soft_band = 0.5
    portfolio_manager.config.regime_rebalance.hard_band = 0.8
    portfolio_manager.config.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.regime_rebalance.flow_trade_min = 200.0
    portfolio_manager.config.regime_rebalance.flow_trade_stop = 100.0

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


@pytest.mark.asyncio
async def test_regime_rebalance_flow_hysteresis_uses_db_state(
    portfolio_manager_with_db, mocker
):
    portfolio_manager_with_db.config.regime_rebalance.soft_band = 0.5
    portfolio_manager_with_db.config.regime_rebalance.hard_band = 0.8
    portfolio_manager_with_db.config.regime_rebalance.choppiness_min = 0.0
    portfolio_manager_with_db.config.regime_rebalance.efficiency_max = 1.0
    portfolio_manager_with_db.config.regime_rebalance.flow_trade_min = 500.0
    portfolio_manager_with_db.config.regime_rebalance.flow_trade_stop = 100.0

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
    portfolio_manager, mocker
):
    portfolio_manager.config.regime_rebalance.soft_band = 1.2
    portfolio_manager.config.regime_rebalance.hard_band = 1.5
    portfolio_manager.config.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.regime_rebalance.deficit_rail_start = 300.0
    portfolio_manager.config.regime_rebalance.deficit_rail_stop = 100.0

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

    assert orders == [("AAA", "NYSE", -4)]


@pytest.mark.asyncio
async def test_regime_rebalance_deficit_rail_sells_pro_rata(portfolio_manager, mocker):
    portfolio_manager.config.regime_rebalance.soft_band = 0.3
    portfolio_manager.config.regime_rebalance.hard_band = 0.8
    portfolio_manager.config.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.regime_rebalance.deficit_rail_start = 100.0
    portfolio_manager.config.regime_rebalance.deficit_rail_stop = 50.0

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
async def test_regime_rebalance_deficit_rail_sells_from_initial_amount(
    portfolio_manager, mocker
):
    portfolio_manager.config.regime_rebalance.soft_band = 1.2
    portfolio_manager.config.regime_rebalance.hard_band = 1.5
    portfolio_manager.config.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.regime_rebalance.deficit_rail_start = 100.0
    portfolio_manager.config.regime_rebalance.deficit_rail_stop = 0.0

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
    portfolio_manager.config.regime_rebalance.soft_band = 0.10
    portfolio_manager.config.regime_rebalance.hard_band = 0.20
    portfolio_manager.config.regime_rebalance.hard_band_rebalance_fraction = 0.5
    portfolio_manager.config.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.regime_rebalance.deficit_rail_start = 500.0
    portfolio_manager.config.regime_rebalance.deficit_rail_stop = 200.0

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
    portfolio_manager.config.regime_rebalance.soft_band = 0.10
    portfolio_manager.config.regime_rebalance.hard_band = 0.20
    portfolio_manager.config.regime_rebalance.choppiness_min = 0.0
    portfolio_manager.config.regime_rebalance.efficiency_max = 1.0
    portfolio_manager.config.regime_rebalance.deficit_rail_start = 100.0
    portfolio_manager.config.regime_rebalance.deficit_rail_stop = 50.0

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
async def test_regime_rebalance_zero_weights_raises(portfolio_manager, mocker):
    account_summary = {"NetLiquidation": SimpleNamespace(value="400")}
    portfolio_positions = {
        "AAA": [SimpleNamespace(contract=Stock("AAA", "SMART", "USD"), position=3)],
        "BBB": [SimpleNamespace(contract=Stock("BBB", "SMART", "USD"), position=1)],
    }
    portfolio_manager.config.symbols["AAA"].weight = 0.0
    portfolio_manager.config.symbols["BBB"].weight = 0.0

    _mock_regime_tickers(portfolio_manager, mocker)
    _mock_regime_history(portfolio_manager, mocker, [100.0, 110.0, 100.0, 110.0])
    portfolio_manager.ibkr.request_executions = mocker.AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="positive target weights"):
        await portfolio_manager.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )
