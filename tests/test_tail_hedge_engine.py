from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from ib_async.contract import Contract, Option, Stock

from thetagang.config import Config
from thetagang.strategies.tail_hedge_engine import (
    TAIL_HEDGE_CLOSE_ORDER_REF,
    TAIL_HEDGE_ENTRY_ORDER_REF,
    TAIL_HEDGE_EVALUATION_EVENT,
    TAIL_HEDGE_SHORT_CLOSE_ORDER_REF,
    TAIL_HEDGE_STATE_EVENT,
    TailHedgeEngine,
)

NOW = datetime(2026, 8, 12, 12, 0, 0)


def _expiration(days: int) -> str:
    return (NOW + timedelta(days=days)).strftime("%Y%m%d")


def _make_engine(mocker):
    tail_config = SimpleNamespace(
        enabled=True,
        symbol="TQQQ",
        annual_budget=0.005,
        entry_vix_max=20.0,
        target_dte=150,
        min_dte=120,
        max_dte=180,
        exit_dte=30,
        long_strike_ratio=0.60,
        short_strike_ratio=0.40,
        minimum_open_interest=50,
        minimum_bid=0.01,
        max_bid_ask_ratio=0.50,
        max_debit_ratio=0.15,
        short_close_profit=0.50,
        short_exit_min_spot_ratio=1.35,
    )
    config = SimpleNamespace(
        strategies=SimpleNamespace(tail_hedge=tail_config),
        portfolio=SimpleNamespace(
            symbols={"TQQQ": SimpleNamespace(primary_exchange="NASDAQ")}
        ),
    )
    ibkr = mocker.Mock()
    ibkr.request_executions = AsyncMock(return_value=[])
    ibkr.open_trades.return_value = []
    order_ops = mocker.Mock()
    order_ops.get_order_exchange.return_value = "SMART"
    order_ops.create_limit_order.return_value = "ORDER"
    data_store = mocker.Mock()
    data_store.get_last_event_payload.return_value = None
    data_store.get_filled_combo_debit.return_value = 0.0
    qualified_contracts: dict[int, Contract] = {}
    engine = TailHedgeEngine(
        config=cast(Config, config),
        ibkr=ibkr,
        order_ops=order_ops,
        data_store=data_store,
        qualified_contracts=qualified_contracts,
        now_provider=lambda: NOW,
    )
    return engine, ibkr, order_ops, data_store, qualified_contracts


async def _manage(engine, positions, *, net_liquidation: float = 20_000.0):
    await engine.manage(positions, net_liquidation=net_liquidation)


def _stock_position(value: float = 20_000.0):
    contract = Stock("TQQQ", "SMART", "USD")
    return SimpleNamespace(
        contract=contract,
        position=500,
        marketValue=value,
        marketPrice=value / 500,
    )


def _option_contract(strike: float, dte: int, con_id: int) -> Option:
    contract = Option("TQQQ", _expiration(dte), strike, "P", "SMART")
    contract.conId = con_id
    contract.multiplier = "100"
    contract.localSymbol = f"TQQQ {strike:g}P"
    return contract


def _option_ticker(contract: Option, bid: float, ask: float, oi: float = 100):
    return SimpleNamespace(
        contract=contract,
        bid=bid,
        ask=ask,
        putOpenInterest=oi,
        midpoint=lambda: (bid + ask) / 2,
        marketPrice=lambda: (bid + ask) / 2,
        modelGreeks=None,
    )


def _state(long_contract: Option, short_contract: Option):
    return {
        "symbol": "TQQQ",
        "long_con_id": long_contract.conId,
        "short_con_id": short_contract.conId,
    }


def _managed_spread_positions(
    long_contract: Option,
    short_contract: Option,
    *,
    stock_value: float | None = None,
    quantity: int = 1,
):
    positions = [
        SimpleNamespace(
            contract=long_contract,
            position=quantity,
            averageCost=100.0,
        ),
        SimpleNamespace(
            contract=short_contract,
            position=-quantity,
            averageCost=30.0,
        ),
    ]
    if stock_value is not None:
        positions.insert(0, _stock_position(stock_value))
    return positions


def _remaining_long_position(long_contract: Option, quantity: int = 1):
    return SimpleNamespace(
        contract=long_contract,
        position=quantity,
        averageCost=100.0,
    )


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


@pytest.mark.parametrize(
    ("long_market", "short_market", "open_interest", "expected"),
    [
        ((0.95, 1.05), (0.25, 0.35), 10, "insufficient_open_interest"),
        ((0.0, 1.0), (0.2, 0.3), 100, "bid_below_minimum"),
        ((0.5, 1.5), (0.25, 0.35), 100, "bid_ask_too_wide"),
        ((5.0, 5.1), (1.0, 1.1), 100, "spread_too_expensive"),
    ],
)
def test_quote_rejection_reports_first_failed_entry_gate(
    mocker,
    long_market,
    short_market,
    open_interest,
    expected,
):
    engine, *_ = _make_engine(mocker)
    long_contract = _option_contract(60, 150, 60)
    short_contract = _option_contract(40, 150, 40)
    quote = engine._build_quote(
        100.0,
        _option_ticker(
            long_contract,
            long_market[0],
            long_market[1],
            open_interest,
        ),
        _option_ticker(
            short_contract,
            short_market[0],
            short_market[1],
            open_interest,
        ),
    )

    assert engine._quote_rejection(quote) == expected


@pytest.mark.parametrize(
    (
        "net_liquidation",
        "rolling_entry_debit",
        "expected_quantity",
        "expected_budget_overage",
    ),
    [
        (100_000.0, 0.0, 7, 0.0),
        (100_000.0, 225.0, 3, 0.0),
        (10_000.0, 0.0, 1, 20.0),
    ],
)
@pytest.mark.asyncio
async def test_favorable_quote_sizes_atomic_spread_from_nlv_budget(
    mocker,
    net_liquidation,
    rolling_entry_debit,
    expected_quantity,
    expected_budget_overage,
):
    engine, ibkr, order_ops, data_store, qualified_contracts = _make_engine(mocker)
    data_store.get_filled_combo_debit.return_value = rolling_entry_debit
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=SimpleNamespace(marketPrice=lambda: 15.0)
    )
    underlying_contract = Stock("TQQQ", "SMART", "USD")
    underlying_contract.conId = 10
    ibkr.get_ticker_for_stock = AsyncMock(
        return_value=SimpleNamespace(
            contract=underlying_contract,
            midpoint=lambda: 100.0,
            marketPrice=lambda: 100.0,
            modelGreeks=None,
        )
    )
    ibkr.get_chains_for_contract = AsyncMock(
        return_value=[
            SimpleNamespace(
                exchange="SMART",
                tradingClass="TQQQ",
                expirations=[_expiration(150)],
                strikes=[40.0, 60.0],
            )
        ]
    )

    async def qualify(*contracts):
        for contract in contracts:
            contract.conId = 60 if contract.strike == 60 else 40
            contract.multiplier = "100"
            contract.localSymbol = f"TQQQ {contract.strike:g}P"
        return list(contracts)

    ibkr.qualify_contracts = AsyncMock(side_effect=qualify)

    async def get_tickers(_symbol, contracts, **_kwargs):
        by_strike = {
            60.0: (0.95, 1.05),
            40.0: (0.25, 0.35),
        }
        return [
            _option_ticker(contract, *by_strike[float(contract.strike)])
            for contract in contracts
        ]

    ibkr.get_tickers_for_contracts = AsyncMock(side_effect=get_tickers)

    await _manage(
        engine,
        {"TQQQ": [_stock_position()]},
        net_liquidation=net_liquidation,
    )

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=expected_quantity,
        limit_price=0.7,
        use_default_algo=False,
        order_ref=TAIL_HEDGE_ENTRY_ORDER_REF,
        transmit=True,
    )
    combo = order_ops.enqueue_order.call_args.args[0]
    assert combo.secType == "BAG"
    assert [(leg.conId, leg.action) for leg in combo.comboLegs] == [
        (60, "BUY"),
        (40, "SELL"),
    ]
    assert qualified_contracts.keys() == {40, 60}
    assert TAIL_HEDGE_STATE_EVENT in [
        call.args[0] for call in data_store.record_event.call_args_list
    ]
    state = _state_events(data_store)[-1]
    assert state["entry_quantity"] == expected_quantity
    assert state["entry_cost"] == pytest.approx(expected_quantity * 70.0)
    assert state["budget_overage"] == pytest.approx(expected_budget_overage)
    evaluation = data_store.record_event.call_args_list[-1].args[1]
    assert evaluation["net_liquidation"] == net_liquidation
    assert evaluation["annual_budget"] == pytest.approx(net_liquidation * 0.005)
    assert evaluation["rolling_entry_debit"] == rolling_entry_debit
    assert _outcomes(data_store) == ["entry_enqueued"]


@pytest.mark.asyncio
async def test_high_vix_leaves_strategy_unhedged(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=SimpleNamespace(marketPrice=lambda: 25.0)
    )

    await _manage(engine, {"TQQQ": [_stock_position()]})

    ibkr.get_ticker_for_stock.assert_not_called()
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["vix_above_entry_max"]


@pytest.mark.asyncio
async def test_unavailable_vix_price_does_not_look_like_cheap_volatility(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=SimpleNamespace(marketPrice=lambda: -1.0)
    )

    await _manage(engine, {"TQQQ": [_stock_position()]})

    ibkr.get_ticker_for_stock.assert_not_called()
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["evaluation_error"]


@pytest.mark.asyncio
async def test_rolling_budget_blocks_entry_before_market_scan(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    data_store.get_filled_combo_debit.return_value = 100.0

    await _manage(engine, {"TQQQ": [_stock_position()]})

    ibkr.get_ticker_for_contract.assert_not_called()
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["annual_budget_exhausted"]


@pytest.mark.asyncio
async def test_working_tail_order_blocks_duplicate_order(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    ibkr.open_trades.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="TQQQ"),
            order=SimpleNamespace(
                orderRef=TAIL_HEDGE_ENTRY_ORDER_REF,
                orderId=17,
                permId=18,
                lmtPrice=0.7,
            ),
            orderStatus=SimpleNamespace(
                status="Submitted",
                filled=0,
                remaining=1,
            ),
        )
    ]

    await _manage(engine, {"TQQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    data_store.get_filled_combo_debit.assert_not_called()
    assert _outcomes(data_store) == ["working_order_present"]


@pytest.mark.parametrize(
    "previous_status",
    ["entry_enqueued", "spread_close_enqueued", "long_close_enqueued"],
)
@pytest.mark.asyncio
async def test_next_run_reconciles_stale_intent_to_empty_broker_positions(
    mocker,
    previous_status,
):
    engine, _ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 150, 60)
    short_contract = _option_contract(40, 150, 40)
    state = _state(long_contract, short_contract)
    state["status"] = previous_status
    data_store.get_last_event_payload.return_value = state
    engine._evaluate_entry = AsyncMock()

    await _manage(engine, {"TQQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    engine._evaluate_entry.assert_awaited_once()
    reconciled_state = _state_events(data_store)[-1]
    assert reconciled_state["status"] == "no_active_hedge"
    assert reconciled_state["long_con_id"] is None
    assert reconciled_state["short_con_id"] is None
    assert reconciled_state["reconciled_from_status"] == previous_status
    assert _outcomes(data_store) == ["state_reconciled_no_active_position"]


@pytest.mark.asyncio
async def test_next_run_retries_an_unfilled_short_close(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 90, 60)
    short_contract = _option_contract(40, 90, 40)
    state = _state(long_contract, short_contract)
    state["status"] = "short_close_enqueued"
    data_store.get_last_event_payload.return_value = state
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_option_ticker(short_contract, 0.09, 0.11)
    )

    await _manage(
        engine, {"TQQQ": _managed_spread_positions(long_contract, short_contract)}
    )

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=1,
        limit_price=0.1,
        order_ref=TAIL_HEDGE_SHORT_CLOSE_ORDER_REF,
        transmit=True,
    )
    assert _state_events(data_store)[-1]["status"] == "short_close_enqueued"


@pytest.mark.asyncio
async def test_recorded_long_only_position_is_managed_safely(mocker):
    engine, _ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 90, 60)
    short_contract = _option_contract(40, 90, 40)
    state = _state(long_contract, short_contract)
    state["status"] = "entry_enqueued"
    data_store.get_last_event_payload.return_value = state

    await _manage(
        engine,
        {
            "TQQQ": [
                SimpleNamespace(
                    contract=long_contract,
                    position=2,
                    averageCost=100.0,
                )
            ]
        },
    )

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["long_put_held"]


@pytest.mark.asyncio
async def test_recorded_short_only_position_is_bought_back(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 90, 60)
    short_contract = _option_contract(40, 90, 40)
    state = _state(long_contract, short_contract)
    state["status"] = "unsafe_short_close_enqueued"
    data_store.get_last_event_payload.return_value = state
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_option_ticker(short_contract, 0.2, 0.3)
    )

    await _manage(
        engine,
        {
            "TQQQ": [
                SimpleNamespace(
                    contract=short_contract,
                    position=-2,
                    averageCost=30.0,
                )
            ]
        },
    )

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=2,
        limit_price=0.25,
        order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        transmit=True,
    )
    close_state = _state_events(data_store)[-1]
    assert close_state["status"] == "unsafe_short_close_enqueued"
    assert close_state["close_reason"] == "long_leg_missing"
    assert _outcomes(data_store) == ["unsafe_short_close_enqueued"]


@pytest.mark.asyncio
async def test_short_quantity_exceeding_long_is_fully_bought_back(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 90, 60)
    short_contract = _option_contract(40, 90, 40)
    data_store.get_last_event_payload.return_value = _state(
        long_contract, short_contract
    )
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_option_ticker(short_contract, 0.2, 0.3)
    )
    positions = [
        _remaining_long_position(long_contract, quantity=2),
        SimpleNamespace(
            contract=short_contract,
            position=-3,
            averageCost=30.0,
        ),
    ]

    await _manage(engine, {"TQQQ": positions})

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=3,
        limit_price=0.25,
        order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        transmit=True,
    )
    close_state = _state_events(data_store)[-1]
    assert close_state["status"] == "unsafe_short_close_enqueued"
    assert close_state["close_reason"] == "short_quantity_exceeds_long"
    assert _outcomes(data_store) == ["unsafe_short_close_enqueued"]


@pytest.mark.asyncio
async def test_negative_position_recorded_as_long_is_bought_back(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 90, 60)
    short_contract = _option_contract(40, 90, 40)
    data_store.get_last_event_payload.return_value = _state(
        long_contract, short_contract
    )
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_option_ticker(long_contract, 0.2, 0.3)
    )
    positions = [
        SimpleNamespace(
            contract=long_contract,
            position=-2,
            averageCost=100.0,
        )
    ]

    await _manage(engine, {"TQQQ": positions})

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=2,
        limit_price=0.25,
        order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        transmit=True,
    )
    close_state = _state_events(data_store)[-1]
    assert close_state["close_reason"] == "recorded_long_is_short"


@pytest.mark.asyncio
async def test_mismatched_expirations_close_the_short_leg(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 90, 60)
    short_contract = _option_contract(40, 60, 40)
    data_store.get_last_event_payload.return_value = _state(
        long_contract, short_contract
    )
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_option_ticker(short_contract, 0.2, 0.3)
    )

    await _manage(
        engine,
        {
            "TQQQ": _managed_spread_positions(
                long_contract,
                short_contract,
                quantity=2,
            )
        },
    )

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=2,
        limit_price=0.25,
        order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        transmit=True,
    )
    close_state = _state_events(data_store)[-1]
    assert close_state["close_reason"] == "managed_expirations_do_not_match"


@pytest.mark.asyncio
async def test_existing_managed_spread_is_held_until_exit_dte(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 90, 60)
    short_contract = _option_contract(40, 90, 40)
    state = _state(long_contract, short_contract)
    state["status"] = "entry_enqueued"
    data_store.get_last_event_payload.return_value = state
    positions = _managed_spread_positions(
        long_contract,
        short_contract,
        stock_value=40_000.0,
    )
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_option_ticker(short_contract, 0.2, 0.3)
    )

    await _manage(engine, {"TQQQ": positions})

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["existing_spread_held"]


@pytest.mark.asyncio
async def test_profitable_short_leg_is_closed_while_long_is_retained(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 90, 60)
    short_contract = _option_contract(40, 90, 40)
    data_store.get_last_event_payload.return_value = _state(
        long_contract, short_contract
    )
    positions = _managed_spread_positions(
        long_contract,
        short_contract,
        quantity=3,
    )
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_option_ticker(short_contract, 0.09, 0.11)
    )

    await _manage(engine, {"TQQQ": positions})

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=3,
        limit_price=0.1,
        order_ref=TAIL_HEDGE_SHORT_CLOSE_ORDER_REF,
        transmit=True,
    )
    order_ops.enqueue_order.assert_called_once_with(short_contract, "ORDER")
    state_events = _state_events(data_store)
    assert state_events[-1]["status"] == "short_close_enqueued"
    assert state_events[-1]["short_quantity"] == 3
    assert state_events[-1]["short_profit_fraction"] == pytest.approx(2 / 3)
    assert _outcomes(data_store) == ["short_close_enqueued"]


@pytest.mark.asyncio
async def test_short_leg_is_closed_when_spot_approaches_its_strike(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 90, 60)
    short_contract = _option_contract(40, 90, 40)
    data_store.get_last_event_payload.return_value = _state(
        long_contract, short_contract
    )
    underlying_contract = Stock("TQQQ", "SMART", "USD")
    underlying_contract.conId = 10
    positions = _managed_spread_positions(
        long_contract,
        short_contract,
        quantity=2,
    )
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_option_ticker(short_contract, 0.39, 0.41)
    )
    ibkr.get_ticker_for_stock = AsyncMock(
        return_value=SimpleNamespace(
            contract=underlying_contract,
            midpoint=lambda: 50.0,
            marketPrice=lambda: 50.0,
            modelGreeks=None,
        )
    )

    await _manage(engine, {"TQQQ": positions})

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=2,
        limit_price=0.4,
        order_ref=TAIL_HEDGE_SHORT_CLOSE_ORDER_REF,
        transmit=True,
    )
    state_events = _state_events(data_store)
    assert state_events[-1]["short_close_reasons"] == ["tail_risk_buffer"]
    assert state_events[-1]["spot_to_short_strike"] == pytest.approx(1.25)
    assert state_events[-1]["estimated_short_financing_profit"] == pytest.approx(-20.0)


@pytest.mark.asyncio
async def test_remaining_long_is_held_after_short_close(mocker):
    engine, _ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 90, 60)
    short_contract = _option_contract(40, 90, 40)
    state = _state(long_contract, short_contract)
    state.update(
        {
            "status": "short_close_enqueued",
            "estimated_short_financing_profit": 20.0,
        }
    )
    data_store.get_last_event_payload.return_value = state

    await _manage(
        engine,
        {"TQQQ": [_remaining_long_position(long_contract, quantity=3)]},
    )

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["long_put_held"]


@pytest.mark.parametrize(
    "previous_status",
    ["short_close_enqueued", "long_close_enqueued"],
)
@pytest.mark.asyncio
async def test_remaining_long_is_closed_or_retried_at_exit_dte(
    mocker,
    previous_status,
):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 20, 60)
    short_contract = _option_contract(40, 90, 40)
    state = _state(long_contract, short_contract)
    state.update(
        {
            "status": previous_status,
            "estimated_short_financing_profit": 20.0,
        }
    )
    data_store.get_last_event_payload.return_value = state
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_option_ticker(long_contract, 4.9, 5.1)
    )

    await _manage(
        engine,
        {"TQQQ": [_remaining_long_position(long_contract, quantity=3)]},
    )

    order_ops.create_limit_order.assert_called_once_with(
        action="SELL",
        quantity=3,
        limit_price=5.0,
        order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        transmit=True,
    )
    assert _state_events(data_store)[-1]["status"] == "long_close_enqueued"
    assert _outcomes(data_store) == ["long_close_enqueued"]


@pytest.mark.asyncio
async def test_nearly_worthless_remaining_long_is_kept_while_new_entry_is_checked(
    mocker,
):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 20, 60)
    short_contract = _option_contract(40, 90, 40)
    state = _state(long_contract, short_contract)
    state["estimated_short_financing_profit"] = 20.0
    data_store.get_last_event_payload.return_value = state
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_option_ticker(long_contract, 0.01, 0.03)
    )
    engine._evaluate_entry = AsyncMock()

    await _manage(
        engine,
        {
            "TQQQ": [
                _stock_position(),
                _remaining_long_position(long_contract, quantity=2),
            ]
        },
    )

    order_ops.enqueue_order.assert_not_called()
    retained_state = _state_events(data_store)[-1]
    assert retained_state["status"] == "lottery_long_retained"
    assert retained_state["long_con_id"] is None
    assert retained_state["short_con_id"] is None
    assert retained_state["lottery_long_con_id"] == long_contract.conId
    assert retained_state["lottery_long_quantity"] == 2
    engine._evaluate_entry.assert_awaited_once_with(
        mocker.ANY,
        net_liquidation=20_000.0,
        previous_state={
            "lottery_long_con_id": long_contract.conId,
            "lottery_long_expiration": long_contract.lastTradeDateOrContractMonth,
            "lottery_long_quantity": 2,
            "lottery_long_strike": 60.0,
            "lottery_long_retained_at": NOW,
            "lottery_long_retained_bid": 0.01,
        },
    )
    assert _outcomes(data_store) == ["lottery_long_retained"]


@pytest.mark.asyncio
async def test_retained_lottery_long_does_not_block_a_new_entry_check(mocker):
    engine, _ibkr, order_ops, data_store, _ = _make_engine(mocker)
    lottery_contract = _option_contract(60, 20, 60)
    data_store.get_last_event_payload.return_value = {
        "symbol": "TQQQ",
        "long_con_id": None,
        "short_con_id": None,
        "lottery_long_con_id": lottery_contract.conId,
        "lottery_long_expiration": lottery_contract.lastTradeDateOrContractMonth,
        "lottery_long_strike": 60.0,
        "lottery_long_retained_at": NOW,
        "lottery_long_retained_bid": 0.01,
    }
    engine._evaluate_entry = AsyncMock()

    await _manage(
        engine,
        {
            "TQQQ": [
                _stock_position(),
                _remaining_long_position(lottery_contract),
            ]
        },
    )

    order_ops.enqueue_order.assert_not_called()
    engine._evaluate_entry.assert_awaited_once_with(
        mocker.ANY,
        net_liquidation=20_000.0,
        previous_state={
            "lottery_long_con_id": lottery_contract.conId,
            "lottery_long_expiration": lottery_contract.lastTradeDateOrContractMonth,
            "lottery_long_strike": 60.0,
            "lottery_long_retained_at": NOW,
            "lottery_long_retained_bid": 0.01,
        },
    )


@pytest.mark.asyncio
async def test_retained_lottery_long_is_closed_before_expiration(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    lottery_contract = _option_contract(60, 1, 60)
    active_long = _option_contract(65, 150, 65)
    active_short = _option_contract(45, 150, 45)
    data_store.get_last_event_payload.return_value = {
        "symbol": "TQQQ",
        "status": "lottery_long_close_enqueued",
        "long_con_id": active_long.conId,
        "short_con_id": active_short.conId,
        "lottery_long_con_id": lottery_contract.conId,
    }
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_option_ticker(lottery_contract, 0.0, 0.02)
    )

    await _manage(
        engine,
        {
            "TQQQ": [
                _stock_position(),
                _remaining_long_position(lottery_contract),
            ]
        },
    )

    order_ops.create_limit_order.assert_called_once_with(
        action="SELL",
        quantity=1,
        limit_price=0.01,
        order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        transmit=True,
    )
    close_state = _state_events(data_store)[-1]
    assert close_state["status"] == "lottery_long_close_enqueued"
    assert close_state["long_con_id"] == active_long.conId
    assert close_state["short_con_id"] == active_short.conId
    assert close_state["lottery_long_con_id"] == lottery_contract.conId
    assert _outcomes(data_store) == ["lottery_long_close_enqueued"]
    data_store.get_filled_combo_debit.assert_not_called()


@pytest.mark.asyncio
async def test_only_one_nearly_worthless_long_is_retained(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    active_long = _option_contract(60, 20, 60)
    active_short = _option_contract(40, 90, 40)
    lottery_long = _option_contract(55, 10, 55)
    state = _state(active_long, active_short)
    state.update(
        {
            "lottery_long_con_id": lottery_long.conId,
            "lottery_long_expiration": lottery_long.lastTradeDateOrContractMonth,
        }
    )
    data_store.get_last_event_payload.return_value = state
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_option_ticker(active_long, 0.0, 0.02)
    )
    engine._evaluate_entry = AsyncMock()

    await _manage(
        engine,
        {
            "TQQQ": [
                _stock_position(),
                _remaining_long_position(active_long),
                _remaining_long_position(lottery_long),
            ]
        },
    )

    order_ops.create_limit_order.assert_called_once_with(
        action="SELL",
        quantity=1,
        limit_price=0.01,
        order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        transmit=True,
    )
    assert _state_events(data_store)[-1]["status"] == "long_close_enqueued"
    engine._evaluate_entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_managed_spread_closes_both_legs_at_exit_dte(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 20, 60)
    short_contract = _option_contract(40, 20, 40)
    state = _state(long_contract, short_contract)
    state["status"] = "spread_close_enqueued"
    data_store.get_last_event_payload.return_value = state
    positions = _managed_spread_positions(
        long_contract,
        short_contract,
        quantity=4,
    )
    ibkr.get_tickers_for_contracts = AsyncMock(
        return_value=[
            _option_ticker(long_contract, 4.9, 5.1),
            _option_ticker(short_contract, 0.9, 1.1),
        ]
    )

    await _manage(engine, {"TQQQ": positions})

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=4,
        limit_price=-4.0,
        use_default_algo=False,
        order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
        transmit=True,
    )
    combo = order_ops.enqueue_order.call_args.args[0]
    assert [(leg.conId, leg.action) for leg in combo.comboLegs] == [
        (60, "SELL"),
        (40, "BUY"),
    ]
    assert _state_events(data_store)[-1]["status"] == "spread_close_enqueued"
    assert _outcomes(data_store) == ["spread_close_enqueued"]


@pytest.mark.asyncio
async def test_exit_closes_unmatched_long_quantity_separately(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    long_contract = _option_contract(60, 20, 60)
    short_contract = _option_contract(40, 20, 40)
    data_store.get_last_event_payload.return_value = _state(
        long_contract, short_contract
    )
    positions = [
        _remaining_long_position(long_contract, quantity=3),
        SimpleNamespace(
            contract=short_contract,
            position=-2,
            averageCost=30.0,
        ),
    ]
    ibkr.get_tickers_for_contracts = AsyncMock(
        return_value=[
            _option_ticker(long_contract, 4.9, 5.1),
            _option_ticker(short_contract, 0.9, 1.1),
        ]
    )

    await _manage(engine, {"TQQQ": positions})

    assert order_ops.create_limit_order.call_args_list == [
        mocker.call(
            action="BUY",
            quantity=2,
            limit_price=-4.0,
            use_default_algo=False,
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
            transmit=True,
        ),
        mocker.call(
            action="SELL",
            quantity=1,
            limit_price=5.0,
            order_ref=TAIL_HEDGE_CLOSE_ORDER_REF,
            transmit=True,
        ),
    ]
    close_state = _state_events(data_store)[-1]
    assert close_state["spread_quantity"] == 2
    assert close_state["excess_long_quantity"] == 1
    assert close_state["excess_long_limit"] == 5.0


@pytest.mark.asyncio
async def test_unmanaged_puts_block_a_new_strategy_spread(mocker):
    engine, ibkr, order_ops, data_store, _ = _make_engine(mocker)
    manual_put = _option_contract(50, 90, 50)

    await _manage(
        engine,
        {"TQQQ": [_stock_position(), SimpleNamespace(contract=manual_put, position=1)]},
    )

    ibkr.get_ticker_for_contract.assert_not_called()
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["unmanaged_put_positions"]
