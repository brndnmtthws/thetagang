from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from ib_async.contract import Option, Stock

from thetagang.config import Config
from thetagang.config_models import TailHedgeTargetConfig
from thetagang.ibkr import RequiredFieldValidationError
from thetagang.strategies.options_engine import (
    OptionsRuntimeServices,
    OptionsStrategyEngine,
)
from thetagang.strategies.tail_hedge_engine import (
    TAIL_HEDGE_CLOSE_ORDER_REF,
    TAIL_HEDGE_ENTRY_ORDER_REF,
    TAIL_HEDGE_EVALUATION_EVENT,
    TAIL_HEDGE_STATE_EVENT,
    NoLaterExpirationError,
    TailHedgeEngine,
    tail_hedge_owned_con_ids,
)

NOW = datetime(2026, 8, 12, 12, 0, 0)


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
    target = _target()
    tail_config = SimpleNamespace(
        enabled=True,
        annual_budget=0.005,
        targets=[target],
    )
    config = SimpleNamespace(
        runtime=SimpleNamespace(account=SimpleNamespace(number="TEST123")),
        strategies=SimpleNamespace(tail_hedge=tail_config),
        portfolio=SimpleNamespace(
            symbols={
                "QQQ": SimpleNamespace(primary_exchange="NASDAQ"),
                "IBIT": SimpleNamespace(primary_exchange="NASDAQ"),
            }
        ),
    )
    ibkr = mocker.Mock()
    ibkr.open_trades.return_value = []
    order_ops = mocker.Mock()
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


def _put_position(
    contract: Option,
    *,
    quantity: int = 1,
    market_value: float = 50.0,
):
    return SimpleNamespace(
        contract=contract,
        position=quantity,
        marketValue=market_value,
        marketPrice=market_value / max(abs(quantity), 1) / 100,
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
        orderStatus=SimpleNamespace(
            status="Submitted",
            filled=0,
            remaining=1,
        ),
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
    tranche = {
        "entry_id": entry_id,
        "symbol": contract.symbol,
        "status": status,
        "con_id": contract.conId,
        "local_symbol": contract.localSymbol,
        "expiration": contract.lastTradeDateOrContractMonth,
        "strike": float(contract.strike),
        "quantity": 1,
        "entry_limit_price": cost / 100,
        "entry_cost": cost,
        "entry_enqueued_at": entered_at,
    }
    history = {
        "entry_id": entry_id,
        "symbol": contract.symbol,
        "entered_at": entered_at,
        "estimated_cost": cost,
    }
    return tranche, history


def _state(*entries: tuple[dict, dict]) -> dict:
    return {
        "schema_version": 1,
        "strategy": "long_put",
        "account": "TEST123",
        "status": "active",
        "tranches": [tranche for tranche, _history in entries],
        "entry_history": [history for _tranche, history in entries],
    }


def _outcomes(data_store) -> list[str]:
    return [
        call.args[1]["outcome"]
        for call in data_store.record_event.call_args_list
        if call.args[0] == TAIL_HEDGE_EVALUATION_EVENT
    ]


def _state_events(data_store) -> list[dict]:
    return [
        call.args[1]
        for call in data_store.record_event.call_args_list
        if call.args[0] == TAIL_HEDGE_STATE_EVENT
    ]


def _configure_entry_quote(
    engine,
    ibkr,
    *,
    symbol: str = "QQQ",
    con_id: int = 60,
    put_cost: float = 0.50,
) -> Option:
    contract = _put_contract(symbol=symbol, con_id=con_id)
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=SimpleNamespace(marketPrice=lambda: 15.0)
    )
    quote = engine._build_quote(
        100.0,
        _put_ticker(contract, put_cost - 0.05, put_cost + 0.05),
    )
    engine._find_put = AsyncMock(return_value=(quote, contract))
    return contract


def test_owned_contract_ids_require_valid_long_put_state() -> None:
    qqq = _put_contract()
    ibit = _put_contract(symbol="IBIT", con_id=160)
    current_state = _state(_entry(qqq), _entry(ibit))
    wrong_state = {
        "schema_version": 2,
        "strategy": "long_put",
        "tranches": [],
    }

    assert tail_hedge_owned_con_ids(
        current_state,
        account_number="TEST123",
    ) == {60, 160}
    with pytest.raises(RuntimeError, match="invalid schema"):
        tail_hedge_owned_con_ids(wrong_state, account_number="TEST123")
    with pytest.raises(RuntimeError, match="invalid schema"):
        tail_hedge_owned_con_ids(cast(dict, []), account_number="TEST123")
    with pytest.raises(RuntimeError, match="different account"):
        tail_hedge_owned_con_ids(current_state, account_number="OTHER")


@pytest.mark.parametrize(
    ("bid", "ask", "open_interest", "max_premium_ratio", "expected"),
    [
        (0.45, 0.55, 10, 0.05, "insufficient_open_interest"),
        (0.0, 0.50, 100, 0.05, "bid_below_minimum"),
        (0.25, 0.75, 100, 0.05, "bid_ask_too_wide"),
        (5.5, 6.5, 100, 0.05, "put_too_expensive"),
        (0.45, 0.55, 100, 0.05, None),
    ],
)
def test_quote_filters_enforce_cheap_convexity(
    mocker,
    bid,
    ask,
    open_interest,
    max_premium_ratio,
    expected,
):
    engine, *_ = _make_engine(mocker)
    target = _target(max_premium_ratio=max_premium_ratio)
    quote = engine._build_quote(
        100.0,
        _put_ticker(_put_contract(), bid, ask, open_interest),
    )

    assert engine._quote_rejection(target, quote) == expected


def test_contract_multiplier_must_be_positive(mocker):
    engine, *_ = _make_engine(mocker)
    contract = _put_contract()
    contract.multiplier = "0"

    with pytest.raises(RuntimeError, match="multiplier is unavailable"):
        engine._multiplier(contract)


@pytest.mark.asyncio
async def test_entry_uses_one_annual_budget_slice_and_buys_only_puts(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _configure_entry_quote(engine, ibkr)
    call_order: list[str] = []

    def record_event(*args, **kwargs):
        if args[0] == TAIL_HEDGE_STATE_EVENT:
            call_order.append("state")
        return True

    data_store.record_event.side_effect = record_event

    def enqueue_order(*_args):
        call_order.append("order")

    order_ops.enqueue_order.side_effect = enqueue_order

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=2,
        limit_price=0.5,
        use_default_algo=False,
        order_ref=TAIL_HEDGE_ENTRY_ORDER_REF,
        transmit=True,
    )
    order_ops.enqueue_order.assert_called_once_with(contract, "ORDER")
    assert call_order == ["state", "order"]
    state = _state_events(data_store)[-1]
    assert state["strategy"] == "long_put"
    assert state["tranches"][0]["entry_cost"] == 100.0
    assert state["tranches"][0]["symbol"] == "QQQ"
    assert state["entry_history"][0]["estimated_cost"] == 100.0
    assert _outcomes(data_store) == ["entry_enqueued"]


@pytest.mark.asyncio
async def test_entry_never_exceeds_its_budget_slice_for_contract_granularity(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    _configure_entry_quote(engine, ibkr)

    await _manage(
        engine,
        {"QQQ": [_stock_position(value=10_000.0)]},
        net_liquidation=10_000.0,
    )

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["tranche_budget_too_small"]


@pytest.mark.asyncio
async def test_entry_is_not_queued_when_ownership_state_cannot_be_persisted(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    _configure_entry_quote(engine, ibkr)
    data_store.record_event.return_value = False

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["evaluation_error"]


@pytest.mark.asyncio
async def test_entry_is_not_queued_when_ownership_state_cannot_be_read(mocker):
    engine, _ibkr, order_ops, data_store = _make_engine(mocker)
    data_store.get_last_event_payload.side_effect = RuntimeError("database unavailable")

    await _manage(engine, {"QQQ": [_stock_position()]})

    data_store.get_last_event_payload.assert_called_once_with(
        TAIL_HEDGE_STATE_EVENT,
        raise_on_error=True,
    )

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["evaluation_error"]


@pytest.mark.asyncio
async def test_state_from_another_account_fails_closed(mocker):
    engine, _ibkr, order_ops, data_store = _make_engine(mocker)
    data_store.get_last_event_payload.return_value = _state()
    data_store.get_last_event_payload.return_value["account"] = "OTHER"

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["evaluation_error"]


@pytest.mark.asyncio
async def test_current_schema_with_malformed_budget_history_fails_closed(mocker):
    engine, _ibkr, order_ops, data_store = _make_engine(mocker)
    data_store.get_last_event_payload.return_value = {
        "schema_version": 1,
        "strategy": "long_put",
        "account": "TEST123",
        "status": "active",
        "tranches": [],
    }

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["evaluation_error"]


@pytest.mark.asyncio
async def test_market_data_validation_failure_is_isolated_to_tail_stage(mocker):
    engine, _ibkr, order_ops, data_store = _make_engine(mocker)
    engine._evaluate_entry = AsyncMock(
        side_effect=RequiredFieldValidationError("market price timed out")
    )

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["evaluation_error"]


@pytest.mark.asyncio
async def test_recent_entry_applies_derived_annual_spacing(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=150)
    data_store.get_last_event_payload.return_value = _state(
        _entry(contract, days_ago=90)
    )
    engine._evaluate_entry = AsyncMock(wraps=engine._evaluate_entry)

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(contract)]},
    )

    ibkr.get_ticker_for_contract.assert_not_called()
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["long_put_held", "tranche_entry_spacing"]


@pytest.mark.asyncio
async def test_rolling_annual_budget_blocks_entry_before_market_scan(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    data_store.get_last_event_payload.return_value = {
        "schema_version": 1,
        "strategy": "long_put",
        "account": "TEST123",
        "status": "active",
        "tranches": [],
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

    ibkr.get_ticker_for_contract.assert_not_called()
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["annual_budget_exhausted"]


@pytest.mark.asyncio
async def test_old_active_tranche_history_is_retained_outside_budget_window(mocker):
    engine, ibkr, _order_ops, data_store = _make_engine(mocker)
    active_contract = _put_contract(strike=70, dte=120, con_id=70)
    data_store.get_last_event_payload.return_value = _state(
        _entry(active_contract, days_ago=400)
    )
    _configure_entry_quote(engine, ibkr)

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(active_contract)]},
    )

    entry_state = _state_events(data_store)[-1]
    assert len(entry_state["tranches"]) == 2
    assert len(entry_state["entry_history"]) == 2
    assert entry_state["entry_history"][0]["entry_id"].startswith("QQQ:70:")


@pytest.mark.asyncio
async def test_annual_tranche_count_blocks_extra_low_cost_entry(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    data_store.get_last_event_payload.return_value = {
        "schema_version": 1,
        "strategy": "long_put",
        "account": "TEST123",
        "status": "active",
        "tranches": [],
        "entry_history": [
            {
                "entry_id": f"closed-entry-{offset}",
                "symbol": "QQQ",
                "entered_at": NOW - timedelta(days=10 + offset * 91),
                "estimated_cost": 50.0,
            }
            for offset in range(4)
        ],
    }

    await _manage(engine, {"QQQ": [_stock_position()]})

    ibkr.get_ticker_for_contract.assert_not_called()
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["annual_tranche_limit"]


@pytest.mark.asyncio
async def test_high_vix_defers_entry(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=SimpleNamespace(marketPrice=lambda: 25.0)
    )

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["vix_above_entry_max"]


@pytest.mark.asyncio
async def test_no_protected_stock_defers_entry_before_market_scan(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)

    await _manage(engine, {"QQQ": []})

    ibkr.get_ticker_for_contract.assert_not_called()
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["no_protected_stock_position"]


@pytest.mark.asyncio
async def test_later_tranche_requires_a_later_expiration(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=120)
    data_store.get_last_event_payload.return_value = _state(
        _entry(contract, days_ago=100)
    )
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=SimpleNamespace(marketPrice=lambda: 15.0)
    )
    engine._find_put = AsyncMock(
        side_effect=NoLaterExpirationError("no later expiration")
    )

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(contract)]},
    )

    engine._find_put.assert_awaited_once_with(
        engine.config.strategies.tail_hedge.targets[0],
        later_than_expiration=contract.lastTradeDateOrContractMonth,
        exclude_con_ids={contract.conId},
    )
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == [
        "long_put_held",
        "no_later_expiration_available",
    ]


@pytest.mark.asyncio
async def test_long_put_is_held_above_roll_dte(mocker):
    engine, _ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=31)
    data_store.get_last_event_payload.return_value = _state(
        _entry(contract, days_ago=100)
    )
    engine._evaluate_entry = AsyncMock()

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(contract, market_value=500.0)]},
    )

    order_ops.enqueue_order.assert_not_called()
    engine._evaluate_entry.assert_awaited_once()
    assert _outcomes(data_store) == ["long_put_held"]


@pytest.mark.asyncio
async def test_long_put_is_closed_at_roll_dte(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=30)
    contract.exchange = ""
    data_store.get_last_event_payload.return_value = _state(
        _entry(contract, days_ago=150)
    )
    ibkr.get_ticker_for_contract = AsyncMock(return_value=_put_ticker(contract))

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(contract, quantity=3)]},
    )

    order_ops.create_limit_order.assert_called_once_with(
        action="SELL",
        quantity=3,
        limit_price=0.5,
        order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        transmit=True,
    )
    assert contract.exchange == "SMART"
    assert _state_events(data_store)[-1]["tranches"][0]["status"] == ("close_enqueued")
    assert _outcomes(data_store) == ["close_enqueued"]


@pytest.mark.asyncio
async def test_all_due_puts_are_closed_in_the_same_run(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    first = _put_contract(strike=60, dte=20, con_id=60)
    second = _put_contract(strike=55, dte=30, con_id=55)
    data_store.get_last_event_payload.return_value = _state(
        _entry(first, days_ago=160),
        _entry(second, days_ago=150),
    )

    async def get_ticker(contract, **_kwargs):
        return _put_ticker(contract)

    ibkr.get_ticker_for_contract = AsyncMock(side_effect=get_ticker)

    await _manage(
        engine,
        {
            "QQQ": [
                _stock_position(),
                _put_position(first),
                _put_position(second),
            ]
        },
    )

    assert order_ops.enqueue_order.call_count == 2
    assert [
        tranche["status"] for tranche in _state_events(data_store)[-1]["tranches"]
    ] == ["close_enqueued", "close_enqueued"]
    assert _outcomes(data_store) == ["close_enqueued", "close_enqueued"]


@pytest.mark.asyncio
async def test_wrong_way_owned_put_is_bought_back(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=120)
    data_store.get_last_event_payload.return_value = _state(
        _entry(contract, days_ago=100)
    )
    ibkr.get_ticker_for_contract = AsyncMock(return_value=_put_ticker(contract))

    await _manage(
        engine,
        {"QQQ": [_put_position(contract, quantity=-2)]},
    )

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=2,
        limit_price=0.5,
        order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        transmit=True,
    )
    assert _outcomes(data_store) == ["close_enqueued"]
    assert _state_events(data_store)[-1]["tranches"][0]["close_reason"] == (
        "owned_put_is_short"
    )


@pytest.mark.asyncio
async def test_close_order_is_not_blocked_by_state_persistence_failure(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=30)
    data_store.get_last_event_payload.return_value = _state(
        _entry(contract, days_ago=150)
    )
    data_store.record_event.return_value = False
    ibkr.get_ticker_for_contract = AsyncMock(return_value=_put_ticker(contract))

    await _manage(engine, {"QQQ": [_put_position(contract)]})

    order_ops.enqueue_order.assert_called_once_with(contract, "ORDER")


@pytest.mark.asyncio
async def test_unmanaged_put_is_ignored_while_tail_entry_proceeds(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    manual_put = _put_contract(strike=50, dte=120, con_id=50)
    tail_put = _configure_entry_quote(engine, ibkr)

    await _manage(
        engine,
        {
            "QQQ": [
                _stock_position(),
                _put_position(manual_put, quantity=-1),
            ]
        },
    )

    order_ops.enqueue_order.assert_called_once_with(tail_put, "ORDER")
    assert _outcomes(data_store) == ["entry_enqueued"]


@pytest.mark.asyncio
async def test_working_tail_order_blocks_duplicate_management(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    ibkr.open_trades.return_value = [
        _working_trade(_put_contract(), TAIL_HEDGE_ENTRY_ORDER_REF)
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["working_order_present"]


@pytest.mark.asyncio
async def test_working_tail_order_for_another_account_is_ignored(mocker):
    engine, ibkr, order_ops, _data_store = _make_engine(mocker)
    other_account_contract = _put_contract(con_id=60)
    candidate = _configure_entry_quote(engine, ibkr, con_id=59)
    ibkr.open_trades.return_value = [
        _working_trade(
            other_account_contract,
            TAIL_HEDGE_ENTRY_ORDER_REF,
            account="OTHER",
        )
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    ibkr.cancel_order.assert_not_called()
    order_ops.enqueue_order.assert_called_once_with(candidate, "ORDER")


@pytest.mark.asyncio
async def test_working_entry_does_not_delay_a_due_close(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    due_contract = _put_contract(dte=30, con_id=60)
    pending_contract = _put_contract(strike=55, dte=200, con_id=55)
    data_store.get_last_event_payload.return_value = _state(
        _entry(due_contract, days_ago=150),
        _entry(pending_contract, days_ago=1, status="entry_enqueued"),
    )
    ibkr.open_trades.return_value = [
        _working_trade(pending_contract, TAIL_HEDGE_ENTRY_ORDER_REF)
    ]
    ibkr.get_ticker_for_contract = AsyncMock(return_value=_put_ticker(due_contract))

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(due_contract)]},
    )

    order_ops.enqueue_order.assert_called_once_with(due_contract, "ORDER")
    assert _outcomes(data_store) == ["close_enqueued"]


@pytest.mark.asyncio
async def test_working_close_does_not_delay_another_due_close(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    first = _put_contract(dte=20, con_id=60)
    second = _put_contract(strike=55, dte=30, con_id=55)
    data_store.get_last_event_payload.return_value = _state(
        _entry(first, days_ago=160),
        _entry(second, days_ago=150),
    )
    ibkr.open_trades.return_value = [_working_trade(first, TAIL_HEDGE_CLOSE_ORDER_REF)]
    ibkr.get_ticker_for_contract = AsyncMock(return_value=_put_ticker(second))

    await _manage(
        engine,
        {
            "QQQ": [
                _stock_position(),
                _put_position(first),
                _put_position(second),
            ]
        },
    )

    order_ops.enqueue_order.assert_called_once_with(second, "ORDER")
    assert _outcomes(data_store) == [
        "working_close_order_present",
        "close_enqueued",
    ]


@pytest.mark.asyncio
async def test_unfilled_entry_is_removed_from_state_and_budget_history(mocker):
    engine, _ibkr, _order_ops, data_store = _make_engine(mocker)
    contract = _put_contract()
    data_store.get_last_event_payload.return_value = _state(
        _entry(contract, days_ago=1, status="entry_enqueued")
    )
    engine._evaluate_entry = AsyncMock()

    await _manage(engine, {"QQQ": [_stock_position()]})

    reconciled = _state_events(data_store)[-1]
    assert reconciled["tranches"] == []
    assert reconciled["entry_history"] == []
    engine._evaluate_entry.assert_awaited_once()


@pytest.mark.asyncio
async def test_filled_entry_is_reconciled_to_active(mocker):
    engine, _ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=120)
    data_store.get_last_event_payload.return_value = _state(
        _entry(contract, days_ago=1, status="entry_enqueued")
    )
    engine._evaluate_entry = AsyncMock()

    await _manage(engine, {"QQQ": [_put_position(contract)]})

    order_ops.enqueue_order.assert_not_called()
    assert _state_events(data_store)[-1]["tranches"][0]["status"] == "active"


@pytest.mark.asyncio
async def test_find_put_targets_roughly_180_dte_and_60_percent_strike(mocker):
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
                expirations=[_expiration(160), _expiration(180), _expiration(200)],
                strikes=[50.0, 55.0, 60.0, 65.0, 70.0],
            )
        ]
    )

    async def qualify(*contracts):
        for contract in contracts:
            contract.conId = int(contract.strike)
            contract.multiplier = "100"
            contract.localSymbol = f"QQQ {contract.strike:g}P"
        return list(contracts)

    ibkr.qualify_contracts = AsyncMock(side_effect=qualify)

    async def get_tickers(_symbol, contracts, **_kwargs):
        return [_put_ticker(contract) for contract in contracts]

    ibkr.get_tickers_for_contracts = AsyncMock(side_effect=get_tickers)

    quote, contract = await engine._find_put(
        _target(),
        later_than_expiration=None,
        exclude_con_ids=set(),
    )

    assert quote.dte == 180
    assert contract.strike == 60.0
    assert quote.premium_ratio == pytest.approx(0.005)


@pytest.mark.asyncio
async def test_find_put_uses_next_liquid_candidate_when_nearest_is_rejected(mocker):
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
                expirations=[_expiration(180)],
                strikes=[59.0, 60.0],
            )
        ]
    )

    async def qualify(*contracts):
        for contract in contracts:
            contract.conId = int(contract.strike)
            contract.multiplier = "100"
            contract.localSymbol = f"QQQ {contract.strike:g}P"
        return list(contracts)

    ibkr.qualify_contracts = AsyncMock(side_effect=qualify)

    async def get_tickers(_symbol, contracts, **_kwargs):
        return [
            _put_ticker(
                contract,
                open_interest=10 if contract.strike == 60 else 100,
            )
            for contract in contracts
        ]

    ibkr.get_tickers_for_contracts = AsyncMock(side_effect=get_tickers)

    quote, contract = await engine._find_put(
        _target(),
        later_than_expiration=None,
        exclude_con_ids=set(),
    )

    assert contract.strike == 59.0
    assert quote.open_interest == 100


@pytest.mark.asyncio
async def test_multiple_targets_enter_independent_tranches_under_one_budget(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    engine.config.strategies.tail_hedge.targets = [
        _target("QQQ", 0.60),
        _target("IBIT", 0.40),
    ]
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=SimpleNamespace(marketPrice=lambda: 15.0)
    )
    contracts = {
        "QQQ": _put_contract(symbol="QQQ", con_id=60),
        "IBIT": _put_contract(symbol="IBIT", con_id=160),
    }

    async def find_put(target, **_kwargs):
        contract = contracts[target.symbol]
        return (
            engine._build_quote(100.0, _put_ticker(contract, 0.20, 0.30)),
            contract,
        )

    engine._find_put = AsyncMock(side_effect=find_put)

    await _manage(
        engine,
        {
            "QQQ": [_stock_position("QQQ")],
            "IBIT": [_stock_position("IBIT")],
        },
    )

    assert [
        call.kwargs["quantity"] for call in order_ops.create_limit_order.call_args_list
    ] == [3, 2]
    assert [call.args[0].symbol for call in order_ops.enqueue_order.call_args_list] == [
        "QQQ",
        "IBIT",
    ]
    assert ibkr.get_ticker_for_contract.await_count == 1
    state = _state_events(data_store)[-1]
    assert [tranche["symbol"] for tranche in state["tranches"]] == [
        "QQQ",
        "IBIT",
    ]
    assert sum(entry["estimated_cost"] for entry in state["entry_history"]) == 125.0


@pytest.mark.asyncio
async def test_target_spacing_does_not_block_another_target(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    engine.config.strategies.tail_hedge.targets = [
        _target("QQQ", 0.50),
        _target("IBIT", 0.50, entry_gate="none"),
    ]
    recent_qqq = _put_contract(con_id=61)
    _tranche, recent_history = _entry(recent_qqq, days_ago=10)
    data_store.get_last_event_payload.return_value = {
        "schema_version": 1,
        "strategy": "long_put",
        "account": "TEST123",
        "status": "active",
        "tranches": [],
        "entry_history": [recent_history],
    }
    ibit_contract = _configure_entry_quote(
        engine,
        ibkr,
        symbol="IBIT",
        con_id=160,
        put_cost=0.25,
    )

    await _manage(
        engine,
        {
            "QQQ": [_stock_position("QQQ")],
            "IBIT": [_stock_position("IBIT")],
        },
    )

    order_ops.enqueue_order.assert_called_once_with(ibit_contract, "ORDER")
    assert _outcomes(data_store) == ["tranche_entry_spacing", "entry_enqueued"]


@pytest.mark.asyncio
async def test_target_can_skip_vix_gate_without_weakening_other_targets(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    engine.config.strategies.tail_hedge.targets = [
        _target("QQQ", 0.50),
        _target("IBIT", 0.50, entry_gate="none"),
    ]
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=SimpleNamespace(marketPrice=lambda: 25.0)
    )
    ibit_contract = _put_contract(symbol="IBIT", con_id=160)

    async def find_put(target, **_kwargs):
        assert target.symbol == "IBIT"
        return engine._build_quote(100.0, _put_ticker(ibit_contract)), ibit_contract

    engine._find_put = AsyncMock(side_effect=find_put)

    await _manage(
        engine,
        {
            "QQQ": [_stock_position("QQQ")],
            "IBIT": [_stock_position("IBIT")],
        },
    )

    order_ops.enqueue_order.assert_called_once_with(ibit_contract, "ORDER")
    assert _outcomes(data_store) == ["vix_above_entry_max", "entry_enqueued"]


@pytest.mark.asyncio
async def test_due_close_only_blocks_entry_for_its_target(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    engine.config.strategies.tail_hedge.targets = [
        _target("QQQ", 0.50),
        _target("IBIT", 0.50, entry_gate="none"),
    ]
    due_contract = _put_contract(symbol="QQQ", dte=30, con_id=60)
    data_store.get_last_event_payload.return_value = _state(
        _entry(due_contract, days_ago=150)
    )
    ibkr.get_ticker_for_contract = AsyncMock(return_value=_put_ticker(due_contract))
    ibit_contract = _put_contract(symbol="IBIT", con_id=160)

    async def find_put(target, **_kwargs):
        assert target.symbol == "IBIT"
        return engine._build_quote(100.0, _put_ticker(ibit_contract)), ibit_contract

    engine._find_put = AsyncMock(side_effect=find_put)

    await _manage(
        engine,
        {
            "QQQ": [
                _stock_position("QQQ"),
                _put_position(due_contract),
            ],
            "IBIT": [_stock_position("IBIT")],
        },
    )

    assert [call.args[0].symbol for call in order_ops.enqueue_order.call_args_list] == [
        "QQQ",
        "IBIT",
    ]
    state = _state_events(data_store)[-1]
    assert [tranche["status"] for tranche in state["tranches"]] == [
        "close_enqueued",
        "entry_enqueued",
    ]


@pytest.mark.asyncio
async def test_removed_target_is_closed_without_blocking_configured_targets(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    engine.config.strategies.tail_hedge.targets = [
        _target("IBIT", 1.0, entry_gate="none")
    ]
    removed_contract = _put_contract(symbol="QQQ", dte=120, con_id=60)
    data_store.get_last_event_payload.return_value = _state(
        _entry(removed_contract, days_ago=100)
    )
    ibkr.get_ticker_for_contract = AsyncMock(return_value=_put_ticker(removed_contract))
    ibit_contract = _put_contract(symbol="IBIT", con_id=160)

    async def find_put(target, **_kwargs):
        return engine._build_quote(100.0, _put_ticker(ibit_contract)), ibit_contract

    engine._find_put = AsyncMock(side_effect=find_put)

    await _manage(
        engine,
        {
            "QQQ": [_put_position(removed_contract)],
            "IBIT": [_stock_position("IBIT")],
        },
    )

    assert [call.args[0].symbol for call in order_ops.enqueue_order.call_args_list] == [
        "QQQ",
        "IBIT",
    ]
    state = _state_events(data_store)[-1]
    assert state["tranches"][0]["close_reason"] == "target_removed"


@pytest.mark.asyncio
async def test_empty_target_set_closes_every_owned_put(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    engine.config.strategies.tail_hedge.targets = []
    contract = _put_contract(dte=120, con_id=60)
    data_store.get_last_event_payload.return_value = _state(_entry(contract))
    ibkr.get_ticker_for_contract = AsyncMock(return_value=_put_ticker(contract))

    await _manage(engine, {"QQQ": [_put_position(contract)]})

    order_ops.create_limit_order.assert_called_once_with(
        action="SELL",
        quantity=1,
        limit_price=0.5,
        order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        transmit=True,
    )
    state = _state_events(data_store)[-1]
    assert state["tranches"][0]["close_reason"] == "target_removed"


@pytest.mark.asyncio
async def test_removed_target_working_entry_is_cancelled(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    engine.config.strategies.tail_hedge.targets = []
    contract = _put_contract(dte=180, con_id=60)
    data_store.get_last_event_payload.return_value = _state(
        _entry(contract, days_ago=1, status="entry_enqueued")
    )
    trade = _working_trade(contract, TAIL_HEDGE_ENTRY_ORDER_REF)
    ibkr.open_trades.return_value = [trade]

    await _manage(engine, {"QQQ": []})

    ibkr.cancel_order.assert_called_once_with(trade.order)
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["entry_cancel_requested"]


@pytest.mark.asyncio
async def test_find_put_excludes_contracts_owned_by_other_strategies(mocker):
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
                expirations=[_expiration(180)],
                strikes=[59.0, 60.0],
            )
        ]
    )

    async def qualify(*contracts):
        for contract in contracts:
            contract.conId = int(contract.strike)
            contract.multiplier = "100"
            contract.localSymbol = f"QQQ {contract.strike:g}P"
        return list(contracts)

    ibkr.qualify_contracts = AsyncMock(side_effect=qualify)
    ibkr.get_tickers_for_contracts = AsyncMock(
        side_effect=lambda _symbol, contracts, **_kwargs: [
            _put_ticker(contract) for contract in contracts
        ]
    )

    _quote, contract = await engine._find_put(
        _target(),
        later_than_expiration=None,
        exclude_con_ids={60},
    )

    assert contract.conId == 59
    await_args = ibkr.get_tickers_for_contracts.await_args
    assert await_args is not None
    requested_contracts = await_args.args[1]
    assert [candidate.conId for candidate in requested_contracts] == [59]


@pytest.mark.asyncio
async def test_entry_excludes_put_contracts_working_at_the_broker(mocker):
    engine, ibkr, order_ops, _data_store = _make_engine(mocker)
    working_contract = _put_contract(con_id=60)
    candidate = _put_contract(strike=59, con_id=59)
    ibkr.open_trades.return_value = [_working_trade(working_contract, "wheel")]
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=SimpleNamespace(marketPrice=lambda: 15.0)
    )
    occupied_at_scan: set[int] = set()

    async def find_put(_target, *, exclude_con_ids, **_kwargs):
        occupied_at_scan.update(exclude_con_ids)
        return engine._build_quote(100.0, _put_ticker(candidate)), candidate

    engine._find_put = AsyncMock(side_effect=find_put)

    await _manage(engine, {"QQQ": [_stock_position()]})

    assert occupied_at_scan == {60}
    order_ops.enqueue_order.assert_called_once_with(candidate, "ORDER")


@pytest.mark.asyncio
async def test_wheel_netting_ignores_state_owned_tail_puts(mocker):
    tail_put = _put_contract(strike=60, dte=180, con_id=60)
    ibit_tail_put = _put_contract(
        strike=40,
        dte=180,
        con_id=160,
        symbol="IBIT",
    )
    short_put = _put_contract(strike=50, dte=90, con_id=50)
    data_store = mocker.Mock()
    data_store.get_last_event_payload.return_value = _state(
        _entry(tail_put),
        _entry(ibit_tail_put),
    )
    config = SimpleNamespace(
        runtime=SimpleNamespace(account=SimpleNamespace(number="TEST123")),
        portfolio=SimpleNamespace(
            symbols={"QQQ": SimpleNamespace(weight=1.0)},
        ),
        strategies=SimpleNamespace(
            tail_hedge=SimpleNamespace(
                enabled=True,
                targets=[
                    SimpleNamespace(symbol="QQQ"),
                    SimpleNamespace(symbol="IBIT"),
                ],
            ),
            cash_management=SimpleNamespace(cash_fund="SGOV"),
            wheel=SimpleNamespace(
                defaults=SimpleNamespace(
                    write_when=SimpleNamespace(calculate_net_contracts=True),
                ),
            ),
        ),
        is_buy_only_rebalancing=lambda _symbol: False,
        trading_is_allowed=lambda _symbol: True,
        can_write_when=lambda _symbol, _right: (True, True),
        get_strike_limit=lambda _symbol, _right: None,
    )
    services = SimpleNamespace(
        get_symbols=lambda: ["QQQ"],
        get_primary_exchange=lambda _symbol: "NASDAQ",
        get_buying_power=lambda _account_summary: 10_000,
        get_maximum_new_contracts_for=AsyncMock(return_value=10),
        get_write_threshold=AsyncMock(return_value=(0.0, 1.0)),
        get_close_price=lambda _ticker: 100.0,
    )
    ibkr = mocker.Mock()
    ibkr.get_ticker_for_stock = AsyncMock(
        return_value=SimpleNamespace(marketPrice=lambda: 100.0),
    )
    engine = OptionsStrategyEngine(
        config=cast(Config, config),
        ibkr=ibkr,
        option_scanner=mocker.Mock(),
        order_ops=mocker.Mock(),
        services=cast(OptionsRuntimeServices, services),
        target_quantities={},
        has_excess_puts=set(),
        has_excess_calls=set(),
        qualified_contracts={},
        data_store=data_store,
    )

    _positions_table, _actions_table, to_write = await engine.check_if_can_write_puts(
        {},
        {
            "QQQ": [
                _put_position(short_put, quantity=-1),
                _put_position(tail_put),
            ]
        },
    )

    assert to_write == []
    data_store.get_last_event_payload.assert_called_once_with(
        TAIL_HEDGE_STATE_EVENT,
        raise_on_error=True,
    )

    data_store.get_last_event_payload.side_effect = RuntimeError("database unavailable")
    ibkr.get_ticker_for_stock.reset_mock()

    _positions_table, _actions_table, to_write = await engine.check_if_can_write_puts(
        {},
        {
            "QQQ": [
                _put_position(short_put, quantity=-1),
                _put_position(tail_put),
            ]
        },
    )

    assert to_write == []
    ibkr.get_ticker_for_stock.assert_not_awaited()


@pytest.mark.asyncio
async def test_wheel_put_paths_exclude_state_owned_tail_contracts(mocker):
    tail_put = _put_contract(con_id=60)
    ibit_tail_put = _put_contract(con_id=160, symbol="IBIT")
    data_store = mocker.Mock()
    data_store.get_last_event_payload.return_value = _state(
        _entry(tail_put),
        _entry(ibit_tail_put),
    )
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            account=SimpleNamespace(number="TEST123"),
            orders=SimpleNamespace(minimum_credit=0.01),
        ),
        strategies=SimpleNamespace(
            tail_hedge=SimpleNamespace(
                enabled=True,
                targets=[
                    SimpleNamespace(symbol="QQQ"),
                    SimpleNamespace(symbol="IBIT"),
                ],
            ),
        ),
    )
    scanner = mocker.Mock()
    replacement = _put_contract(strike=55, dte=45, con_id=55)
    scanner.find_eligible_contracts = AsyncMock(
        return_value=_put_ticker(replacement),
    )
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

    assert engine.get_short_puts({"QQQ": [_put_position(tail_put, quantity=-1)]}) == []
    await engine.write_puts([("QQQ", "NASDAQ", 1, None)])

    await_args = scanner.find_eligible_contracts.await_args
    assert await_args is not None
    assert await_args.kwargs["exclude_con_ids"] == {60, 160}
    order_ops.enqueue_order.assert_called_once_with(replacement, "ORDER")
