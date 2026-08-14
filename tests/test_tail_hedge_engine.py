from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from ib_async.contract import Option, Stock
from ib_async.wrapper import RequestError

from thetagang.config import Config
from thetagang.config_models import TailHedgeTargetConfig
from thetagang.db import DataStore
from thetagang.strategies.options_engine import (
    OptionsRuntimeServices,
    OptionsStrategyEngine,
)
from thetagang.strategies.tail_hedge_engine import (
    TAIL_HEDGE_CLOSE_ORDER_REF,
    TAIL_HEDGE_ENTRY_ORDER_REF,
    TAIL_HEDGE_EVALUATION_EVENT,
    TailHedgeEngine,
)
from thetagang.strategies.tail_hedge_state import (
    TAIL_HEDGE_STATE_EVENT,
    TAIL_HEDGE_STATE_SCHEMA_VERSION,
    TailHedgeStateStore,
)

NOW = datetime(2026, 8, 12, 12)


def _expiration(days: int) -> str:
    return (NOW + timedelta(days=days)).strftime("%Y%m%d")


def _target(
    symbol: str = "QQQ",
    budget_weight: float = 1.0,
    **overrides,
) -> TailHedgeTargetConfig:
    return TailHedgeTargetConfig(
        symbol=symbol,
        budget_weight=budget_weight,
        **overrides,
    )


def _make_engine(mocker):
    config = SimpleNamespace(
        runtime=SimpleNamespace(account=SimpleNamespace(number="TEST123")),
        strategies=SimpleNamespace(
            tail_hedge=SimpleNamespace(
                enabled=True,
                annual_budget=0.005,
                targets=[_target()],
            )
        ),
        portfolio=SimpleNamespace(
            symbols={
                "QQQ": SimpleNamespace(primary_exchange="NASDAQ"),
                "IBIT": SimpleNamespace(primary_exchange="NASDAQ"),
            }
        ),
    )
    config.trading_is_allowed = lambda symbol: not bool(
        getattr(config.portfolio.symbols.get(symbol), "no_trading", False)
    )
    ibkr = mocker.Mock()
    ibkr.open_trades.return_value = []
    order_ops = mocker.Mock()
    order_ops.orders.records.return_value = []
    order_ops.get_order_exchange.return_value = "SMART"
    order_ops.create_limit_order.return_value = "ORDER"
    data_store = mocker.Mock()
    data_store.get_last_event_payload.return_value = None
    data_store.record_event.return_value = True
    engine = TailHedgeEngine(
        config=cast(Config, config),
        ibkr=ibkr,
        order_ops=order_ops,
        data_store=data_store,
        now_provider=lambda: NOW,
    )
    return engine, ibkr, order_ops, data_store


async def _manage(engine, positions, *, net_liquidation: float = 100_000.0):
    if engine.ibkr.cached_net_liquidation.side_effect is None:
        engine.ibkr.cached_net_liquidation.return_value = net_liquidation
    if engine.ibkr.portfolio.side_effect is None:
        engine.ibkr.portfolio.return_value = [
            position
            for symbol_positions in positions.values()
            for position in symbol_positions
        ]
    await engine.manage(positions, net_liquidation=net_liquidation)


def _stock_position(symbol: str = "QQQ", value: float = 100_000.0):
    return SimpleNamespace(
        contract=Stock(symbol, "SMART", "USD"),
        position=1_000,
        marketValue=value,
        marketPrice=value / 1_000,
    )


def _put_contract(
    strike: float = 60,
    dte: int = 180,
    con_id: int = 60,
    symbol: str = "QQQ",
) -> Option:
    contract = Option(symbol, _expiration(dte), strike, "P", "SMART")
    contract.conId = con_id
    contract.multiplier = "100"
    contract.localSymbol = (
        f"{symbol} {strike:g}P {contract.lastTradeDateOrContractMonth}"
    )
    return contract


def _put_ticker(
    contract: Option,
    bid: float = 0.45,
    ask: float = 0.55,
    open_interest: float = 100,
):
    return SimpleNamespace(
        contract=contract,
        bid=bid,
        ask=ask,
        putOpenInterest=open_interest,
        midpoint=lambda: (bid + ask) / 2,
        marketPrice=lambda: (bid + ask) / 2,
        modelGreeks=None,
    )


def _put_position(contract: Option, *, quantity: int = 1, value: float = 50.0):
    return SimpleNamespace(
        contract=contract,
        position=quantity,
        marketValue=value,
        marketPrice=value / max(abs(quantity), 1) / 100,
        averageCost=50.0,
    )


def _working_trade(
    contract: Option,
    order_ref: str,
    *,
    account: str = "TEST123",
):
    return SimpleNamespace(
        contract=contract,
        order=SimpleNamespace(
            orderRef=order_ref,
            orderId=17,
            lmtPrice=0.5,
            account=account,
        ),
        orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=1),
    )


def _entry(
    contract: Option,
    *,
    days_ago: int = 100,
    status: str = "active",
    cost: float = 50.0,
) -> tuple[dict, dict]:
    entered_at = NOW - timedelta(days=days_ago)
    entry_id = f"{contract.symbol}:{contract.conId}:{entered_at.isoformat()}"
    return (
        {
            "entry_id": entry_id,
            "symbol": contract.symbol,
            "status": status,
            "con_id": contract.conId,
            "local_symbol": contract.localSymbol,
            "expiration": contract.lastTradeDateOrContractMonth,
            "strike": float(contract.strike),
            "quantity": 1,
            "entry_limit_price": cost / 100,
            "entry_enqueued_at": entered_at,
        },
        {
            "entry_id": entry_id,
            "symbol": contract.symbol,
            "entered_at": entered_at,
            "estimated_cost": cost,
        },
    )


def _state(*entries: tuple[dict, dict]) -> dict:
    return {
        "schema_version": TAIL_HEDGE_STATE_SCHEMA_VERSION,
        "strategy": "long_put",
        "account": "TEST123",
        "status": "active",
        "tranches": [tranche for tranche, _history in entries],
        "entry_history": [history for _tranche, history in entries],
    }


def _events(data_store, event_type: str) -> list[dict]:
    return [
        call.args[1]
        for call in data_store.record_event.call_args_list
        if call.args[0] == event_type
    ]


def _outcomes(data_store) -> list[str]:
    return [
        event["outcome"] for event in _events(data_store, TAIL_HEDGE_EVALUATION_EVENT)
    ]


def _configure_entry(engine, ibkr, *, con_id: int = 60) -> Option:
    contract = _put_contract(con_id=con_id)
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=SimpleNamespace(marketPrice=lambda: 15.0)
    )
    engine._find_put = AsyncMock(
        return_value=(engine._build_quote(100.0, _put_ticker(contract)), contract)
    )
    return contract


def test_state_ownership_survives_config_rename_and_isolates_accounts(tmp_path) -> None:
    contract = _put_contract()
    db_url = f"sqlite:///{tmp_path / 'state.db'}"
    original = DataStore(
        db_url,
        str(tmp_path / "old.toml"),
        dry_run=False,
        config_text="old",
    )
    assert original.record_event(
        TAIL_HEDGE_STATE_EVENT,
        _state(_entry(contract)),
        symbol="TEST123",
    )
    renamed = DataStore(
        db_url,
        str(tmp_path / "renamed.toml"),
        dry_run=False,
        config_text="renamed",
    )

    assert TailHedgeStateStore(renamed, "TEST123").load().owned_con_ids == {60}
    assert TailHedgeStateStore(renamed, "OTHER").load().owned_con_ids == set()


@pytest.mark.parametrize(
    ("field", "value"),
    [("quantity", 0), ("entry_limit_price", float("nan"))],
)
def test_state_rejects_invalid_owned_quantity_or_price(mocker, field, value) -> None:
    payload = _state(_entry(_put_contract()))
    payload["tranches"][0][field] = value
    data_store = mocker.Mock()
    data_store.get_last_event_payload.return_value = payload

    with pytest.raises(RuntimeError, match="invalid tranche"):
        TailHedgeStateStore(data_store, "TEST123").load()


@pytest.mark.parametrize(
    ("bid", "ask", "open_interest", "expected"),
    [
        (0.45, 0.55, 10, "insufficient_open_interest"),
        (0.0, 0.50, 100, "bid_below_minimum"),
        (0.25, 0.75, 100, "bid_ask_too_wide"),
        (5.5, 6.5, 100, "put_too_expensive"),
        (0.45, 0.55, 100, None),
    ],
)
def test_quote_filters_enforce_cheap_convexity(
    mocker,
    bid,
    ask,
    open_interest,
    expected,
):
    engine, *_ = _make_engine(mocker)
    quote = engine._build_quote(
        100.0,
        _put_ticker(_put_contract(), bid, ask, open_interest),
    )

    assert engine._quote_rejection(_target(), quote) == expected


@pytest.mark.parametrize("multiplier", ["", "0"])
def test_contract_multiplier_fails_closed(mocker, multiplier):
    engine, *_ = _make_engine(mocker)
    contract = _put_contract()
    contract.multiplier = multiplier

    with pytest.raises(RuntimeError, match="multiplier is unavailable"):
        engine._multiplier(contract)


@pytest.mark.asyncio
async def test_entry_rechecks_budget_after_quote_and_persists_before_queue(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _configure_entry(engine, ibkr)
    call_order = []
    quote_result = engine._find_put.return_value

    async def lower_net_liquidation(*_args, **_kwargs):
        ibkr.cached_net_liquidation.return_value = 50_000.0
        return quote_result

    def record_event(event_type, *_args, **_kwargs):
        if event_type == TAIL_HEDGE_STATE_EVENT:
            call_order.append("state")
        return True

    data_store.record_event.side_effect = record_event
    engine._find_put.side_effect = lower_net_liquidation
    order_ops.enqueue_order.side_effect = lambda *_args: call_order.append("order")

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=1,
        limit_price=0.5,
        use_default_algo=False,
        order_ref=TAIL_HEDGE_ENTRY_ORDER_REF,
        transmit=True,
    )
    order_ops.enqueue_order.assert_called_once_with(contract, "ORDER")
    state = _events(data_store, TAIL_HEDGE_STATE_EVENT)[-1]
    assert call_order == ["state", "order"]
    assert state["tranches"][0]["entry_limit_price"] == 0.5
    assert state["entry_history"][0]["estimated_cost"] == 50.0
    ibkr.cached_net_liquidation.assert_called_once_with("TEST123")


@pytest.mark.asyncio
async def test_minimum_entry_spacing_prevents_catch_up_bunching(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract()
    _tranche, history = _entry(contract, days_ago=1)
    payload = _state()
    payload["entry_history"] = [history]
    data_store.get_last_event_payload.return_value = payload

    await _manage(engine, {"QQQ": [_stock_position()]})

    ibkr.get_ticker_for_contract.assert_not_called()
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["minimum_entry_spacing"]


@pytest.mark.asyncio
async def test_entry_rechecks_live_stock_after_market_data_awaits(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    _configure_entry(engine, ibkr)
    stock = _stock_position()
    ibkr.portfolio.side_effect = [[stock], [stock], []]

    await _manage(engine, {"QQQ": [stock]})

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["protected_position_changed"]


@pytest.mark.asyncio
async def test_entry_fails_closed_when_state_cannot_be_saved(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    _configure_entry(engine, ibkr)
    data_store.record_event.return_value = False

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["evaluation_error"]


@pytest.mark.asyncio
async def test_request_error_is_isolated_to_tail_stage(mocker):
    engine, _ibkr, order_ops, data_store = _make_engine(mocker)
    engine._evaluate_entry = AsyncMock(
        side_effect=RequestError(17, 200, "No security definition")
    )

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    assert (
        _events(data_store, TAIL_HEDGE_EVALUATION_EVENT)[-1]["error_type"]
        == "RequestError"
    )


@pytest.mark.asyncio
async def test_gross_annual_budget_is_not_refunded_after_exit(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    _configure_entry(engine, ibkr)
    data_store.get_last_event_payload.return_value = {
        **_state(),
        "entry_history": [
            {
                "entry_id": "closed-entry",
                "symbol": "QQQ",
                "entered_at": NOW - timedelta(days=100),
                "estimated_cost": 500.0,
            }
        ],
    }

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    engine._find_put.assert_not_awaited()
    assert _outcomes(data_store) == ["annual_budget_exhausted"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "outcome"),
    [
        ("high_vix", "vix_above_entry_max"),
        ("no_stock", "no_protected_stock_position"),
        ("no_trading", "trading_disabled"),
        ("too_expensive", "contract_exceeds_applicable_budget"),
    ],
)
async def test_entry_gates_leave_intentional_coverage_gaps(mocker, case, outcome):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    positions = {"QQQ": [_stock_position()]}
    net_liquidation = 100_000.0
    if case == "high_vix":
        ibkr.get_ticker_for_contract = AsyncMock(
            return_value=SimpleNamespace(marketPrice=lambda: 25.0)
        )
    elif case == "no_stock":
        positions = {"QQQ": []}
    elif case == "no_trading":
        engine.config.portfolio.symbols["QQQ"].no_trading = True
    else:
        _configure_entry(engine, ibkr)
        net_liquidation = 10_000.0

    await _manage(engine, positions, net_liquidation=net_liquidation)

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == [outcome]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("removed", "dte", "reason"),
    [(False, 30, "roll_dte"), (True, 180, "target_removed")],
)
async def test_due_and_removed_puts_are_closed(mocker, removed, dte, reason):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=dte)
    data_store.get_last_event_payload.return_value = _state(_entry(contract))
    ibkr.get_ticker_for_contract = AsyncMock(return_value=_put_ticker(contract))
    if removed:
        engine.config.strategies.tail_hedge.targets = []

    await _manage(engine, {"QQQ": [_stock_position(), _put_position(contract)]})

    order_ops.create_limit_order.assert_called_once_with(
        action="SELL",
        quantity=1,
        limit_price=0.5,
        order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        transmit=True,
    )
    assert (
        _events(data_store, TAIL_HEDGE_EVALUATION_EVENT)[-1]["close_reason"] == reason
    )


@pytest.mark.asyncio
async def test_working_order_for_another_account_does_not_block_entry(mocker):
    engine, ibkr, order_ops, _data_store = _make_engine(mocker)
    candidate = _configure_entry(engine, ibkr, con_id=59)
    ibkr.open_trades.return_value = [
        _working_trade(
            _put_contract(con_id=60),
            TAIL_HEDGE_ENTRY_ORDER_REF,
            account="OTHER",
        )
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_called_once_with(candidate, "ORDER")


@pytest.mark.asyncio
async def test_close_rechecks_live_position_after_quote_await(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=30)
    stale_position = _put_position(contract)
    data_store.get_last_event_payload.return_value = _state(_entry(contract))
    close_filled = False

    def portfolio(**_kwargs):
        return [] if close_filled else [stale_position]

    async def get_ticker(_contract, **_kwargs):
        nonlocal close_filled
        close_filled = True
        return _put_ticker(contract)

    ibkr.portfolio.side_effect = portfolio
    ibkr.get_ticker_for_contract = AsyncMock(side_effect=get_ticker)

    await _manage(engine, {"QQQ": [_stock_position(), stale_position]})

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["position_changed_before_close"]


@pytest.mark.asyncio
async def test_put_selection_is_deterministic_and_separates_expirations(mocker):
    engine, ibkr, _order_ops, _data_store = _make_engine(mocker)
    underlying = Stock("QQQ", "SMART", "USD")
    underlying.conId = 10
    ibkr.get_ticker_for_stock = AsyncMock(
        return_value=SimpleNamespace(
            contract=underlying,
            midpoint=lambda: 100.0,
            marketPrice=lambda: 100.0,
            modelGreeks=None,
        )
    )
    ibkr.get_chains_for_contract = AsyncMock(
        return_value=[
            SimpleNamespace(
                exchange="SMART",
                tradingClass="QQQ",
                multiplier="100",
                expirations=[_expiration(160), _expiration(180), _expiration(200)],
                strikes=[55.0, 59.0, 60.0, 65.0],
            )
        ]
    )

    async def qualify(*contracts):
        assert all(
            contract.tradingClass == "QQQ" and contract.multiplier == "100"
            for contract in contracts
        )
        for con_id, contract in enumerate(contracts, start=1):
            contract.conId = con_id
            contract.multiplier = "100"
            contract.localSymbol = f"QQQ {contract.strike:g}P"
        return list(contracts)

    ibkr.qualify_contracts = AsyncMock(side_effect=qualify)
    ibkr.get_tickers_for_contracts = AsyncMock(
        side_effect=lambda _symbol, contracts, **_kwargs: [
            _put_ticker(contract) for contract in reversed(contracts)
        ]
    )

    quote, contract = await engine._find_put(
        _target(),
        latest_expiration=None,
        exclude_con_ids=set(),
    )

    assert quote.dte == 180
    assert contract.strike == 60.0

    separated_quote, _contract = await engine._find_put(
        _target(),
        latest_expiration=_expiration(109),
        exclude_con_ids=set(),
    )

    assert separated_quote.dte == 200


@pytest.mark.asyncio
async def test_scanner_excludes_live_and_working_put_contracts(mocker):
    engine, ibkr, order_ops, _data_store = _make_engine(mocker)
    candidate = _configure_entry(engine, ibkr, con_id=59)
    ibkr.open_trades.return_value = [_working_trade(_put_contract(con_id=60), "wheel")]
    occupied = set()

    async def find_put(_target, *, exclude_con_ids, **_kwargs):
        occupied.update(exclude_con_ids)
        return engine._build_quote(100.0, _put_ticker(candidate)), candidate

    engine._find_put = AsyncMock(side_effect=find_put)

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(_put_contract(con_id=50))]},
    )

    assert occupied == {50, 60}
    order_ops.enqueue_order.assert_called_once_with(candidate, "ORDER")


@pytest.mark.asyncio
async def test_wheel_put_write_excludes_state_owned_contracts(mocker):
    tail_put = _put_contract(con_id=60)
    data_store = mocker.Mock()
    data_store.get_last_event_payload.return_value = _state(_entry(tail_put))
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            account=SimpleNamespace(number="TEST123"),
            orders=SimpleNamespace(minimum_credit=0.01),
        ),
        strategies=SimpleNamespace(
            tail_hedge=SimpleNamespace(enabled=True, targets=[_target()])
        ),
    )
    replacement = _put_contract(strike=55, con_id=55)
    scanner = mocker.Mock()
    scanner.find_eligible_contracts = AsyncMock(return_value=_put_ticker(replacement))
    order_ops = mocker.Mock()
    order_ops.get_order_exchange.return_value = "SMART"
    order_ops.create_limit_order.return_value = "ORDER"
    engine = OptionsStrategyEngine(
        config=cast(Config, config),
        ibkr=mocker.Mock(),
        option_scanner=scanner,
        order_ops=order_ops,
        services=cast(OptionsRuntimeServices, SimpleNamespace()),
        target_quantities={},
        has_excess_puts=set(),
        has_excess_calls=set(),
        qualified_contracts={},
        data_store=data_store,
    )

    await engine.write_puts([("QQQ", "NASDAQ", 1, None)])

    await_args = scanner.find_eligible_contracts.await_args
    assert await_args is not None
    assert await_args.kwargs["exclude_con_ids"] == {60}
    order_ops.enqueue_order.assert_called_once_with(replacement, "ORDER")
