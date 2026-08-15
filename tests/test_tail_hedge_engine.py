from datetime import datetime, timedelta, timezone
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
    NoEligibleExpirationError,
    TailHedgeEngine,
)
from thetagang.strategies.tail_hedge_state import (
    TailHedgeCohort,
    TailHedgeState,
    TailHedgeStateStore,
    TailHedgeStatus,
    build_tail_reduction_order_ref,
    parse_state_datetime,
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
    ibkr.trades.return_value = []
    order_ops = mocker.Mock()
    order_ops.orders.records.return_value = []
    order_ops.get_order_exchange.return_value = "SMART"
    order_ops.create_limit_order.return_value = "ORDER"
    data_store = mocker.Mock()
    data_store.load_tail_hedge_entries.return_value = []
    data_store.save_tail_hedge_entries.return_value = True
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
    action: str | None = None,
    status: str = "Submitted",
    filled: float = 0,
    remaining: float = 1,
    order_id: int = 17,
    observed_at: datetime | None = NOW,
):
    if action is None:
        action = "BUY" if order_ref == TAIL_HEDGE_ENTRY_ORDER_REF else "SELL"
    return SimpleNamespace(
        contract=contract,
        order=SimpleNamespace(
            orderRef=order_ref,
            orderId=order_id,
            lmtPrice=0.5,
            account=account,
            action=action,
        ),
        orderStatus=SimpleNamespace(
            status=status,
            filled=filled,
            remaining=remaining,
        ),
        log=([] if observed_at is None else [SimpleNamespace(time=observed_at)]),
        fills=[],
    )


def _entry(
    contract: Option,
    *,
    days_ago: int = 100,
    status: TailHedgeStatus = "active",
    cost: float = 50.0,
    quantity: int = 1,
    recovered_cost: float = 0.0,
    pending_recovery_quantity: int | None = None,
    pending_recovery_per_contract: float | None = None,
    pending_recovery_enqueued_at: datetime | None = None,
    pending_recovery_initial_quantity: int | None = None,
) -> TailHedgeCohort:
    entered_at = NOW - timedelta(days=days_ago)
    entry_id = f"{contract.symbol}:{contract.conId}:{entered_at.isoformat()}"
    return TailHedgeCohort(
        entry_id=entry_id,
        symbol=contract.symbol,
        status=status,
        con_id=contract.conId,
        expiration=contract.lastTradeDateOrContractMonth,
        strike=float(contract.strike),
        quantity=quantity,
        entry_limit_price=cost / quantity / 100,
        entered_at=entered_at,
        estimated_cost=cost,
        recovered_cost=recovered_cost,
        pending_recovery_quantity=pending_recovery_quantity,
        pending_recovery_per_contract=pending_recovery_per_contract,
        pending_recovery_enqueued_at=(
            pending_recovery_enqueued_at or NOW
            if pending_recovery_quantity is not None
            else None
        ),
        pending_recovery_initial_quantity=(
            pending_recovery_initial_quantity or pending_recovery_quantity
            if pending_recovery_quantity is not None
            else None
        ),
    )


def _state(*cohorts: TailHedgeCohort) -> TailHedgeState:
    return TailHedgeState(list(cohorts))


def _state_rows(state: TailHedgeState) -> list[dict]:
    return state.to_rows()


def _saved_states(data_store) -> list[TailHedgeState]:
    return [
        TailHedgeState.from_rows(call.args[1])
        for call in data_store.save_tail_hedge_entries.call_args_list
    ]


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
    state = _state(_entry(contract))
    TailHedgeStateStore(original, "TEST123").save(state)
    renamed = DataStore(
        db_url,
        str(tmp_path / "renamed.toml"),
        dry_run=False,
        config_text="renamed",
    )

    assert TailHedgeStateStore(renamed, "TEST123").load().owned_con_ids == {60}
    assert TailHedgeStateStore(renamed, "OTHER").load().owned_con_ids == set()


def test_state_store_matches_recovery_to_final_submission_quantity(tmp_path) -> None:
    contract = _put_contract()
    data_store = DataStore(
        f"sqlite:///{tmp_path / 'submission.db'}",
        str(tmp_path / "config.toml"),
        dry_run=False,
        config_text="config",
    )
    pending = _entry(
        contract,
        cost=300.0,
        quantity=3,
        pending_recovery_quantity=3,
        pending_recovery_per_contract=100.0,
    )
    store = TailHedgeStateStore(data_store, "TEST123")
    store.save(_state(pending))

    assert store.update_recovery_submission(60, 1, live_quantity=2)
    resized = store.load().open_cohorts[0]
    assert resized.quantity == 2
    assert resized.recovered_cost == 0.0
    assert resized.pending_recovery_quantity == 1
    assert resized.pending_recovery_initial_quantity == 1

    assert store.update_recovery_submission(60, None)
    assert not store.load().open_cohorts[0].has_pending_recovery


def test_state_store_releases_only_matching_pending_entry(tmp_path) -> None:
    data_store = DataStore(
        f"sqlite:///{tmp_path / 'release-entry.db'}",
        str(tmp_path / "config.toml"),
        dry_run=False,
        config_text="config",
    )
    pending_contract = _put_contract(con_id=60)
    active_contract = _put_contract(con_id=61)
    store = TailHedgeStateStore(data_store, "TEST123")
    store.save(
        _state(
            _entry(pending_contract, status="entry_enqueued"),
            _entry(active_contract),
        )
    )

    assert store.release_entry_submission(60)
    assert {cohort.con_id for cohort in store.load().cohorts} == {61}
    assert not store.release_entry_submission(61)
    assert {cohort.con_id for cohort in store.load().cohorts} == {61}


def test_broker_timestamps_are_normalized_to_local_state_time() -> None:
    local_offset = datetime.now().astimezone().utcoffset() or timedelta()
    foreign_offset = timedelta(hours=-12 if local_offset >= timedelta() else 14)
    observed = datetime(2026, 8, 12, 12, tzinfo=timezone(foreign_offset))

    assert parse_state_datetime(observed) == observed.astimezone().replace(tzinfo=None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", 0),
        ("entry_limit_price", float("nan")),
        ("estimated_cost", "50"),
    ],
)
def test_state_rejects_invalid_cohort_values(mocker, field, value) -> None:
    rows = _state_rows(_state(_entry(_put_contract())))
    rows[0][field] = value
    data_store = mocker.Mock()
    data_store.load_tail_hedge_entries.return_value = rows

    with pytest.raises(RuntimeError, match="invalid cohort"):
        TailHedgeStateStore(data_store, "TEST123").load()


def test_state_rejects_recovery_over_entry_cost(mocker) -> None:
    rows = _state_rows(_state(_entry(_put_contract(), cost=50.0)))
    rows[0]["recovered_cost"] = 50.01
    data_store = mocker.Mock()
    data_store.load_tail_hedge_entries.return_value = rows

    with pytest.raises(RuntimeError, match="invalid cohort"):
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
        ibkr.cached_net_liquidation.return_value = 100_000.0
        return quote_result

    def save_tail_state(*_args, **_kwargs):
        call_order.append("state")
        return True

    data_store.save_tail_hedge_entries.side_effect = save_tail_state
    engine._find_put.side_effect = lower_net_liquidation
    order_ops.enqueue_order.side_effect = lambda *_args: call_order.append("order")

    await _manage(
        engine,
        {"QQQ": [_stock_position()]},
        net_liquidation=200_000.0,
    )

    order_ops.create_limit_order.assert_called_once_with(
        action="BUY",
        quantity=1,
        limit_price=0.5,
        use_default_algo=False,
        order_ref=TAIL_HEDGE_ENTRY_ORDER_REF,
        transmit=True,
    )
    order_ops.enqueue_order.assert_called_once_with(contract, "ORDER")
    state = _saved_states(data_store)[-1]
    assert call_order == ["state", "order"]
    assert state.open_cohorts[0].entry_limit_price == 0.5
    assert state.open_cohorts[0].estimated_cost == 50.0
    assert state.open_cohorts[0].recovered_cost == 0.0
    ibkr.cached_net_liquidation.assert_called_once_with("TEST123")


@pytest.mark.asyncio
async def test_minimum_entry_spacing_prevents_catch_up_bunching(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract()
    recent = _entry(contract, days_ago=1, status="closed")
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(recent))

    await _manage(engine, {"QQQ": [_stock_position()]})

    ibkr.get_ticker_for_contract.assert_not_called()
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["minimum_entry_spacing"]


def test_default_cadence_models_six_entries_and_two_to_three_live_cohorts():
    target = _target()
    cadence = target.minimum_entry_spacing_days
    holding_days = target.target_dte - target.exit_dte
    entry_offsets = range(-2 * cadence, 365, cadence)

    entry_dates = [NOW + timedelta(days=offset) for offset in entry_offsets]
    annual_entries = [
        entry for entry in entry_dates if NOW <= entry < NOW + timedelta(days=365)
    ]
    live_counts = [
        sum(
            entry <= day < entry + timedelta(days=holding_days) for entry in entry_dates
        )
        for day in (NOW + timedelta(days=offset) for offset in range(365))
    ]

    assert (target.target_dte, target.exit_dte, cadence, holding_days) == (
        180,
        30,
        61,
        150,
    )
    assert len(annual_entries) == 6
    assert set(live_counts) == {2, 3}


@pytest.mark.asyncio
async def test_entry_count_does_not_impose_a_second_annual_limit(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    candidate = _configure_entry(engine, ibkr, con_id=70)
    state = _state(
        *(
            _entry(
                _put_contract(con_id=con_id),
                days_ago=days_ago,
                status="closed",
                cost=1.0,
            )
            for con_id, days_ago in enumerate(
                (61, 120, 180, 240, 300, 360),
                start=1,
            )
        )
    )
    data_store.load_tail_hedge_entries.return_value = _state_rows(state)

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_called_once_with(candidate, "ORDER")
    assert "annual_tranche_limit" not in _outcomes(data_store)


@pytest.mark.asyncio
async def test_canceled_zero_fill_entry_releases_its_cadence_reservation(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    canceled_contract = _put_contract(con_id=60)
    canceled = _entry(canceled_contract, days_ago=1, status="entry_enqueued")
    candidate = _configure_entry(engine, ibkr, con_id=59)
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(canceled))

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_called_once_with(candidate, "ORDER")
    saved_state = _saved_states(data_store)[-1]
    assert [cohort.entry_id for cohort in saved_state.cohorts] == [
        saved_state.open_cohorts[0].entry_id
    ]
    assert saved_state.open_cohorts[0].con_id == 59


@pytest.mark.asyncio
async def test_completed_entry_fill_waits_for_portfolio_cache(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(con_id=60)
    entry = _entry(contract, days_ago=0, status="entry_enqueued")
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(entry))
    ibkr.trades.return_value = [
        _working_trade(
            contract,
            TAIL_HEDGE_ENTRY_ORDER_REF,
            status="Filled",
            filled=1,
            remaining=0,
        )
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["working_order_present"]
    assert _saved_states(data_store) == []


@pytest.mark.asyncio
async def test_multi_contract_entry_waits_for_complete_portfolio_cache(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(con_id=60)
    entry = _entry(
        contract,
        days_ago=0,
        status="entry_enqueued",
        cost=300.0,
        quantity=3,
    )
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(entry))
    ibkr.trades.return_value = [
        _working_trade(
            contract,
            TAIL_HEDGE_ENTRY_ORDER_REF,
            status="Filled",
            filled=3,
            remaining=0,
        )
    ]

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(contract, quantity=1)]},
    )

    order_ops.enqueue_order.assert_not_called()
    assert _saved_states(data_store) == []

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(contract, quantity=3)]},
    )

    activated = _saved_states(data_store)[-1].open_cohorts[0]
    assert activated.status == "active"
    assert activated.quantity == 3
    assert activated.estimated_cost == 300.0


@pytest.mark.asyncio
async def test_late_entry_quantity_growth_restores_budget_charge(mocker):
    engine, _ibkr, _order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(con_id=60)
    entry = _entry(contract, days_ago=1, cost=100.0, quantity=1)
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(entry))

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(contract, quantity=3)]},
    )

    reconciled = _saved_states(data_store)[-1].open_cohorts[0]
    assert reconciled.quantity == 3
    assert reconciled.estimated_cost == 300.0


@pytest.mark.asyncio
async def test_orphaned_completed_entry_does_not_freeze_future_entries(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    orphaned_contract = _put_contract(con_id=60)
    orphaned = _entry(
        orphaned_contract,
        days_ago=62,
        status="entry_enqueued",
    )
    candidate = _configure_entry(engine, ibkr, con_id=59)
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(orphaned))
    ibkr.trades.return_value = [
        _working_trade(
            orphaned_contract,
            TAIL_HEDGE_ENTRY_ORDER_REF,
            status="Filled",
            filled=1,
            remaining=0,
            observed_at=NOW - timedelta(days=62),
        )
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_called_once_with(candidate, "ORDER")
    saved_state = _saved_states(data_store)[-1]
    assert saved_state.open_cohorts[0].con_id == 59
    assert len(saved_state.cohorts) == 2
    assert {cohort.status for cohort in saved_state.cohorts} == {
        "closed",
        "entry_enqueued",
    }


@pytest.mark.asyncio
async def test_terminal_zero_fill_cancel_overrides_reconciliation_grace(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    canceled_contract = _put_contract(con_id=60)
    canceled = _entry(
        canceled_contract,
        days_ago=0,
        status="entry_enqueued",
    )
    candidate = _configure_entry(engine, ibkr, con_id=59)
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(canceled))
    ibkr.trades.return_value = [
        _working_trade(
            canceled_contract,
            TAIL_HEDGE_ENTRY_ORDER_REF,
            status="Cancelled",
            filled=0,
            remaining=1,
        )
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_called_once_with(candidate, "ORDER")
    saved_state = _saved_states(data_store)[-1]
    assert saved_state.open_cohorts[0].con_id == 59


@pytest.mark.asyncio
async def test_missing_expiration_retries_once_on_the_next_run(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    candidate = _configure_entry(engine, ibkr)
    quote_result = engine._find_put.return_value
    engine._find_put.side_effect = [
        NoEligibleExpirationError(
            "No option expiration is inside the configured DTE range"
        ),
        quote_result,
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    assert _saved_states(data_store) == []
    assert _outcomes(data_store) == ["no_eligible_expiration_available"]

    await _manage(engine, {"QQQ": [_stock_position()]})

    assert engine._find_put.await_count == 2
    order_ops.enqueue_order.assert_called_once_with(candidate, "ORDER")
    assert len(_saved_states(data_store)) == 1


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
async def test_entry_rechecks_selected_contract_occupancy_after_quote(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _configure_entry(engine, ibkr)
    stock = _stock_position()
    occupied_put = _put_position(contract)
    ibkr.portfolio.side_effect = [
        [stock],
        [stock],
        [stock, occupied_put],
    ]

    await _manage(engine, {"QQQ": [stock]})

    data_store.save_tail_hedge_entries.assert_not_called()
    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["target_put_became_occupied"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "order_ref"),
    [("BUY", None), ("SELL", "ordinary-stock-order")],
)
async def test_any_same_run_stock_order_blocks_tail_entry(
    mocker,
    action,
    order_ref,
):
    engine, ibkr, order_ops, _data_store = _make_engine(mocker)
    _configure_entry(engine, ibkr)
    stock = Stock("QQQ", "SMART", "USD")
    order_ops.orders.records.return_value = [
        (
            stock,
            SimpleNamespace(action=action, orderRef=order_ref),
            None,
        )
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    engine._find_put.assert_not_awaited()
    order_ops.enqueue_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["BUY", "SELL"])
async def test_any_working_broker_stock_order_blocks_tail_entry(mocker, action):
    engine, ibkr, order_ops, _data_store = _make_engine(mocker)
    _configure_entry(engine, ibkr)
    stock = Stock("QQQ", "SMART", "USD")
    ibkr.open_trades.return_value = [
        SimpleNamespace(
            contract=stock,
            order=SimpleNamespace(account="TEST123", action=action, orderRef="stock"),
            isDone=lambda: False,
        )
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    engine._find_put.assert_not_awaited()
    order_ops.enqueue_order.assert_not_called()


@pytest.mark.asyncio
async def test_entry_fails_closed_when_state_cannot_be_saved(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    _configure_entry(engine, ibkr)
    data_store.save_tail_hedge_entries.return_value = False

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    assert _outcomes(data_store) == ["evaluation_error"]


@pytest.mark.asyncio
async def test_close_fails_closed_when_recovery_intent_cannot_be_saved(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=30)
    data_store.load_tail_hedge_entries.return_value = _state_rows(
        _state(_entry(contract))
    )
    data_store.save_tail_hedge_entries.return_value = False
    ibkr.get_ticker_for_contract = AsyncMock(return_value=_put_ticker(contract))

    await _manage(engine, {"QQQ": [_stock_position(), _put_position(contract)]})

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
@pytest.mark.parametrize(
    ("recovered_cost", "expected_outcome"),
    [(0.0, "annual_budget_exhausted"), (450.0, "entry_enqueued")],
)
async def test_annual_budget_uses_net_cost_but_keeps_a_fixed_entry_slice(
    mocker,
    recovered_cost,
    expected_outcome,
):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    candidate = _configure_entry(engine, ibkr)
    prior = _entry(
        _put_contract(con_id=99),
        days_ago=100,
        status="closed",
        cost=500.0,
        recovered_cost=recovered_cost,
    )
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(prior))

    await _manage(engine, {"QQQ": [_stock_position()]})

    assert _outcomes(data_store) == [expected_outcome]
    if recovered_cost == 0:
        order_ops.enqueue_order.assert_not_called()
        engine._find_put.assert_not_awaited()
    else:
        order_ops.create_limit_order.assert_called_once_with(
            action="BUY",
            quantity=1,
            limit_price=0.5,
            use_default_algo=False,
            order_ref=TAIL_HEDGE_ENTRY_ORDER_REF,
            transmit=True,
        )
        order_ops.enqueue_order.assert_called_once_with(candidate, "ORDER")


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
    data_store.load_tail_hedge_entries.return_value = _state_rows(
        _state(_entry(contract))
    )
    ibkr.get_ticker_for_contract = AsyncMock(return_value=_put_ticker(contract))
    if removed:
        engine.config.strategies.tail_hedge.targets = []

    await _manage(engine, {"QQQ": [_stock_position(), _put_position(contract)]})

    order_ops.create_limit_order.assert_called_once_with(
        action="SELL",
        quantity=1,
        limit_price=0.5,
        order_ref=build_tail_reduction_order_ref(
            TAIL_HEDGE_CLOSE_ORDER_REF,
            contract.conId,
            NOW,
        ),
        transmit=True,
    )
    assert (
        _events(data_store, TAIL_HEDGE_EVALUATION_EVENT)[-1]["close_reason"] == reason
    )


@pytest.mark.asyncio
async def test_exit_recovery_waits_for_observed_reduction_and_never_overcredits(
    mocker,
):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=30)
    entry = _entry(contract, days_ago=1, cost=150.0, quantity=2)
    full_position = _put_position(contract, quantity=2, value=200.0)
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(entry))
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_put_ticker(contract, bid=0.95, ask=1.05)
    )
    call_order = []

    def save_tail_state(*_args, **_kwargs):
        call_order.append("state")
        return True

    data_store.save_tail_hedge_entries.side_effect = save_tail_state
    order_ops.enqueue_order.side_effect = lambda *_args: call_order.append("order")

    await _manage(engine, {"QQQ": [_stock_position(), full_position]})

    close_pending_state = _saved_states(data_store)[-1]
    close_pending = close_pending_state.open_cohorts[0]
    assert call_order == ["state", "order"]
    assert close_pending.recovered_cost == 0.0
    assert close_pending.pending_recovery_quantity == 2
    assert close_pending.pending_recovery_per_contract == 100.0
    assert close_pending.pending_recovery_enqueued_at == NOW
    assert close_pending.pending_recovery_initial_quantity == 2

    ibkr.open_trades.return_value = [
        _working_trade(contract, TAIL_HEDGE_CLOSE_ORDER_REF)
    ]
    data_store.load_tail_hedge_entries.return_value = _state_rows(close_pending_state)
    partial_position = _put_position(contract, quantity=1, value=100.0)

    await _manage(engine, {"QQQ": [_stock_position(), partial_position]})

    partial_state = _saved_states(data_store)[-1]
    partial = partial_state.open_cohorts[0]
    assert partial.recovered_cost == 100.0
    assert partial.quantity == 1
    assert partial.pending_recovery_initial_quantity == 2

    data_store.load_tail_hedge_entries.return_value = _state_rows(partial_state)
    state_event_count = len(_saved_states(data_store))

    await _manage(engine, {"QQQ": [_stock_position(), partial_position]})

    repeated_events = _saved_states(data_store)[state_event_count:]
    assert all(event.cohorts[0].recovered_cost == 100.0 for event in repeated_events)

    data_store.load_tail_hedge_entries.return_value = _state_rows(
        repeated_events[-1] if repeated_events else partial_state
    )
    ibkr.open_trades.return_value = []
    ibkr.trades.return_value = [
        _working_trade(
            contract,
            TAIL_HEDGE_CLOSE_ORDER_REF,
            status="Filled",
            filled=2,
            remaining=0,
        )
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    closed_state = _saved_states(data_store)[-1]
    assert closed_state.open_cohorts == []
    assert closed_state.cohorts[0].recovered_cost == 150.0


@pytest.mark.asyncio
async def test_completed_exit_fill_waits_for_portfolio_cache(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=30)
    pending = _entry(
        contract,
        days_ago=1,
        cost=250.0,
        quantity=2,
        pending_recovery_quantity=2,
        pending_recovery_per_contract=100.0,
        pending_recovery_enqueued_at=NOW,
        pending_recovery_initial_quantity=2,
    )
    pending_state = _state(pending)
    data_store.load_tail_hedge_entries.return_value = _state_rows(pending_state)
    ibkr.trades.return_value = [
        _working_trade(
            contract,
            TAIL_HEDGE_CLOSE_ORDER_REF,
            status="Filled",
            filled=2,
            remaining=0,
        )
    ]

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(contract, quantity=2)]},
    )

    order_ops.enqueue_order.assert_not_called()
    assert _saved_states(data_store) == []

    await _manage(engine, {"QQQ": [_stock_position()]})

    closed_state = _saved_states(data_store)[-1]
    assert closed_state.open_cohorts == []
    assert closed_state.cohorts[0].recovered_cost == 200.0


@pytest.mark.asyncio
async def test_restored_timestamp_less_fill_matches_persisted_recovery_ref(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=30)
    recovery_enqueued_at = NOW - timedelta(days=1)
    pending = _entry(
        contract,
        days_ago=1,
        cost=250.0,
        quantity=2,
        pending_recovery_quantity=2,
        pending_recovery_per_contract=100.0,
        pending_recovery_enqueued_at=recovery_enqueued_at,
        pending_recovery_initial_quantity=2,
    )
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(pending))
    ibkr.trades.return_value = [
        _working_trade(
            contract,
            build_tail_reduction_order_ref(
                TAIL_HEDGE_CLOSE_ORDER_REF,
                contract.conId,
                recovery_enqueued_at,
            ),
            status="Filled",
            filled=0,
            remaining=0,
            observed_at=None,
        )
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    closed = _saved_states(data_store)[-1].cohorts[0]
    assert closed.status == "closed"
    assert closed.recovered_cost == 200.0


@pytest.mark.asyncio
async def test_timestamp_less_legacy_fill_cannot_credit_new_recovery(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=30)
    recovery_enqueued_at = NOW - timedelta(days=1)
    pending = _entry(
        contract,
        days_ago=1,
        cost=250.0,
        quantity=2,
        pending_recovery_quantity=2,
        pending_recovery_per_contract=100.0,
        pending_recovery_enqueued_at=recovery_enqueued_at,
        pending_recovery_initial_quantity=2,
    )
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(pending))
    ibkr.trades.return_value = [
        _working_trade(
            contract,
            TAIL_HEDGE_CLOSE_ORDER_REF,
            status="Filled",
            filled=0,
            remaining=0,
            observed_at=None,
        )
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    order_ops.enqueue_order.assert_not_called()
    closed = _saved_states(data_store)[-1].cohorts[0]
    assert closed.status == "closed"
    assert closed.recovered_cost == 0.0


@pytest.mark.asyncio
async def test_aged_unsubmitted_close_intent_requeues_without_budget_credit(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=30)
    pending = _entry(
        contract,
        days_ago=1,
        cost=250.0,
        quantity=2,
        pending_recovery_quantity=2,
        pending_recovery_per_contract=100.0,
        pending_recovery_enqueued_at=NOW - timedelta(days=1),
        pending_recovery_initial_quantity=2,
    )
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(pending))
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=_put_ticker(contract, bid=0.95, ask=1.05)
    )

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(contract, quantity=2)]},
    )

    order_ops.enqueue_order.assert_called_once_with(contract, "ORDER")
    retried = _saved_states(data_store)[-1].open_cohorts[0]
    assert retried.recovered_cost == 0.0
    assert retried.pending_recovery_quantity == 2
    assert retried.pending_recovery_enqueued_at == NOW


@pytest.mark.asyncio
async def test_canceled_exit_with_missing_position_does_not_refund_budget(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=30)
    pending = _entry(
        contract,
        days_ago=1,
        cost=250.0,
        quantity=2,
        pending_recovery_quantity=2,
        pending_recovery_per_contract=100.0,
        pending_recovery_enqueued_at=NOW,
        pending_recovery_initial_quantity=2,
    )
    data_store.load_tail_hedge_entries.return_value = _state_rows(_state(pending))
    ibkr.trades.return_value = [
        _working_trade(
            contract,
            TAIL_HEDGE_CLOSE_ORDER_REF,
            status="Cancelled",
            filled=0,
            remaining=2,
        )
    ]

    await _manage(engine, {"QQQ": [_stock_position()]})

    closed_state = _saved_states(data_store)[-1]
    assert closed_state.open_cohorts == []
    assert closed_state.cohorts[0].recovered_cost == 0.0
    order_ops.enqueue_order.assert_not_called()


@pytest.mark.asyncio
async def test_partially_filled_cancel_clears_only_after_reduction_is_observed(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=180)
    pending = _entry(
        contract,
        days_ago=1,
        cost=250.0,
        quantity=2,
        pending_recovery_quantity=2,
        pending_recovery_per_contract=100.0,
        pending_recovery_enqueued_at=NOW,
        pending_recovery_initial_quantity=2,
    )
    pending_state = _state(pending)
    data_store.load_tail_hedge_entries.return_value = _state_rows(pending_state)
    ibkr.trades.return_value = [
        _working_trade(
            contract,
            TAIL_HEDGE_CLOSE_ORDER_REF,
            status="Cancelled",
            filled=1,
            remaining=1,
        )
    ]

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(contract, quantity=2)]},
    )

    order_ops.enqueue_order.assert_not_called()
    assert _saved_states(data_store) == []

    await _manage(
        engine,
        {"QQQ": [_stock_position(), _put_position(contract, quantity=1)]},
    )

    reconciled = _saved_states(data_store)[-1]
    assert reconciled.open_cohorts[0].recovered_cost == 100.0
    assert not reconciled.open_cohorts[0].has_pending_recovery
    order_ops.enqueue_order.assert_not_called()


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
    data_store.load_tail_hedge_entries.return_value = _state_rows(
        _state(_entry(contract))
    )
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
async def test_close_does_not_credit_a_pre_submission_position_change(mocker):
    engine, ibkr, order_ops, data_store = _make_engine(mocker)
    contract = _put_contract(dte=30)
    stale_position = _put_position(contract, quantity=2)
    live_position = _put_position(contract, quantity=1)
    data_store.load_tail_hedge_entries.return_value = _state_rows(
        _state(_entry(contract, cost=200.0, quantity=2))
    )
    quote_returned = False

    def portfolio(**_kwargs):
        return [live_position] if quote_returned else [stale_position]

    async def get_ticker(_contract, **_kwargs):
        nonlocal quote_returned
        quote_returned = True
        return _put_ticker(contract, bid=0.95, ask=1.05)

    ibkr.portfolio.side_effect = portfolio
    ibkr.get_ticker_for_contract = AsyncMock(side_effect=get_ticker)

    await _manage(engine, {"QQQ": [_stock_position(), stale_position]})

    order_ops.enqueue_order.assert_called_once_with(contract, "ORDER")
    pending = _saved_states(data_store)[-1].open_cohorts[0]
    assert pending.quantity == 1
    assert pending.recovered_cost == 0.0
    assert pending.pending_recovery_quantity == 1


@pytest.mark.asyncio
async def test_put_selection_is_deterministic_and_allows_same_expiry(mocker):
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
        exclude_con_ids=set(),
    )

    assert quote.dte == 180
    assert contract.strike == 60.0

    same_expiry_quote, same_expiry_contract = await engine._find_put(
        _target(),
        exclude_con_ids={contract.conId},
    )

    assert same_expiry_quote.expiration == quote.expiration
    assert same_expiry_contract.conId != contract.conId
    assert same_expiry_contract.strike == 59.0


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
    data_store.load_tail_hedge_entries.return_value = _state_rows(
        _state(_entry(tail_put))
    )
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
