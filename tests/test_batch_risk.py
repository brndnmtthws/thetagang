import asyncio
from copy import copy
from unittest.mock import Mock

import pytest
from ib_async import (
    AccountValue,
    ComboLeg,
    Contract,
    LimitOrder,
    Option,
    OrderState,
    OrderStatus,
    PortfolioItem,
    Stock,
    Trade,
)

from thetagang.batch_risk import (
    BatchRiskError,
    margin_cost,
    pending_debit,
    projected_positions,
    remaining_orders,
    validate_batch,
)
from thetagang.ibkr import IBKR, DryRunOrderError

ACCOUNT = "DU-TEST"


def summary(funds=1000, power=1000):
    return {
        tag: AccountValue(ACCOUNT, tag, str(value), "USD", "")
        for tag, value in (
            ("AvailableFunds", funds),
            ("ExcessLiquidity", funds),
            ("BuyingPower", power),
        )
    }


def stock_order(symbol="SPY", quantity=1, action="BUY", price=100):
    return Stock(symbol, "SMART", "USD"), LimitOrder(
        action, quantity, price, account=ACCOUNT
    )


def position(contract, quantity):
    return PortfolioItem(contract, quantity, 100, quantity * 100, 100, 0, 0, ACCOUNT)


def validate(
    proposed, *, positions=(), working=(), qualified=None, funds=1000, power=1000
):
    return validate_batch(
        account=ACCOUNT,
        summary=summary(funds, power),
        positions=positions,
        proposed=proposed,
        working=working,
        qualified=qualified or {},
    )


def test_combined_cross_strategy_debits_fail_even_if_each_order_fits():
    first, second = stock_order(quantity=6), stock_order("QQQ", 6)
    assert validate([first]).debit == 600
    assert validate([second]).debit == 600
    with pytest.raises(BatchRiskError, match="Combined pending"):
        validate([first, second])


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_duplicate_and_opposing_contract_orders_are_rejected(action):
    with pytest.raises(BatchRiskError, match="Duplicate or opposing"):
        validate([stock_order(), stock_order(action=action)])


def test_partial_working_orders_reserve_only_remaining_quantity():
    contract, order = stock_order(quantity=10)
    working = Trade(
        contract, order, OrderStatus(status="Submitted", filled=6, remaining=4)
    )
    assert remaining_orders([working], ACCOUNT)[0][1].totalQuantity == 4
    assert order.totalQuantity == 10
    assert validate([stock_order("QQQ", 6)], working=[working]).debit == 1000
    with pytest.raises(BatchRiskError):
        validate([stock_order("QQQ", 7)], working=[working])
    with pytest.raises(BatchRiskError, match="Duplicate"):
        validate([stock_order()], working=[working])


def test_foreign_and_terminal_orders_do_not_reserve_funds():
    contract, order = stock_order(quantity=100)
    order.account = "DU-OTHER"
    foreign = Trade(contract, order, OrderStatus(status="Submitted"))
    closed = Trade(contract, copy(order), OrderStatus(status="Filled"))
    closed.order.account = ACCOUNT
    assert validate([stock_order()], working=[foreign, closed]).debit == 100


def test_stock_sales_do_not_fund_pending_buys():
    sell = stock_order(quantity=10, action="SELL")
    with pytest.raises(BatchRiskError, match="pending debits"):
        validate([sell, stock_order("QQQ", 11)], positions=[position(sell[0], 10)])


def test_queued_buys_cannot_back_stock_sales_or_covered_calls():
    with pytest.raises(BatchRiskError, match="actual unreserved"):
        validate([stock_order(quantity=1, action="SELL")])
    call = Option(
        "SPY", "20261218", 100, "C", "SMART", multiplier="100", currency="USD"
    )
    sell_call = (call, LimitOrder("SELL", 1, 2, account=ACCOUNT))
    with pytest.raises(BatchRiskError, match="Call coverage"):
        validate([stock_order(quantity=100), sell_call], power=20000)
    assert (
        validate(
            [sell_call], positions=[position(Stock("SPY", "SMART", "USD"), 100)]
        ).debit
        == 0
    )


def test_stock_sale_cannot_remove_existing_short_call_cover():
    contract, sell = stock_order(quantity=1, action="SELL")
    call = Option(
        "SPY", "20261218", 100, "C", "SMART", multiplier="100", currency="USD"
    )
    with pytest.raises(BatchRiskError, match="Call coverage"):
        validate(
            [(contract, sell)], positions=[position(contract, 100), position(call, -1)]
        )


def combo():
    old = Option(
        "SPY", "20261218", 100, "C", "SMART", multiplier="100", currency="USD", conId=1
    )
    new = Option(
        "SPY", "20270115", 110, "C", "SMART", multiplier="100", currency="USD", conId=2
    )
    bag = Contract(
        secType="BAG",
        symbol="SPY",
        currency="USD",
        comboLegs=[
            ComboLeg(conId=1, ratio=1, action="BUY", exchange="SMART"),
            ComboLeg(conId=2, ratio=1, action="SELL", exchange="SMART"),
        ],
    )
    return bag, {1: old, 2: new}


def test_combo_roll_keeps_cover_and_handles_credit_sign():
    bag, qualified = combo()
    buy = LimitOrder("BUY", 1, -1, account=ACCOUNT)
    assert pending_debit(bag, buy, qualified) == 0
    assert pending_debit(bag, LimitOrder("SELL", 1, -1), qualified) == 100
    result = validate(
        [(bag, buy)],
        qualified=qualified,
        positions=[
            position(Stock("SPY", "SMART", "USD"), 100),
            position(qualified[1], -1),
        ],
    )
    assert result.debit == 0
    with pytest.raises(BatchRiskError, match="Unqualified"):
        pending_debit(bag, buy, {})
    qualified[2].multiplier = "10"
    with pytest.raises(BatchRiskError, match="Mixed combo"):
        pending_debit(bag, buy, qualified)


def test_roll_and_standalone_close_cannot_trade_same_leg_twice():
    bag, qualified = combo()
    with pytest.raises(BatchRiskError, match="Duplicate"):
        validate(
            [
                (bag, LimitOrder("BUY", 1, -1, account=ACCOUNT)),
                (qualified[1], LimitOrder("BUY", 1, 2, account=ACCOUNT)),
            ],
            qualified=qualified,
            positions=[
                position(Stock("SPY", "SMART", "USD"), 100),
                position(qualified[1], -1),
            ],
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("totalQuantity", 0),
        ("totalQuantity", -1),
        ("totalQuantity", float("nan")),
        ("lmtPrice", float("inf")),
        ("lmtPrice", 0),
        ("orderType", "MKT"),
    ],
)
def test_invalid_order_data_is_rejected(field, value):
    contract, order = stock_order()
    setattr(order, field, value)
    with pytest.raises(BatchRiskError):
        validate([(contract, order)])


@pytest.mark.parametrize("tag", ["AvailableFunds", "ExcessLiquidity", "BuyingPower"])
def test_missing_account_risk_values_fail_closed(tag):
    values = summary()
    del values[tag]
    with pytest.raises(BatchRiskError, match="Missing"):
        validate_batch(
            account=ACCOUNT,
            summary=values,
            positions=[],
            proposed=[stock_order()],
            working=[],
            qualified={},
        )


def test_currency_and_unknown_working_status_fail_closed():
    contract, order = stock_order()
    contract.currency = "EUR"
    with pytest.raises(BatchRiskError, match="currency"):
        validate([(contract, order)])
    contract.currency = "USD"
    with pytest.raises(BatchRiskError, match="unresolved"):
        validate(
            [stock_order("QQQ")],
            working=[Trade(contract, order, OrderStatus(status="PendingSubmit"))],
        )


def test_projection_includes_queued_exposure_without_mutating_actual_positions():
    contract, order = stock_order(quantity=2)
    held = position(contract, 3)
    projected = projected_positions(
        {"SPY": [held]}, [(contract, order), stock_order("QQQ", 4)], {}
    )
    assert held.position == 3
    assert projected["SPY"][0].position == 5
    assert projected["QQQ"][0].position == 4
    assert projected["QQQ"][0].marketValue == 400
    management = projected_positions(
        {"SPY": [held]},
        [(contract, order), stock_order("QQQ", 4)],
        {},
        include_new=False,
    )
    assert management["SPY"][0].position == 3
    assert "QQQ" not in management


def test_margin_releases_do_not_offset_other_orders():
    release = OrderState(
        initMarginChange="-1000", maintMarginChange="-800", equityWithLoanChange="100"
    )
    assert margin_cost(release, "USD", 5) == (5, 5)
    debit = OrderState(
        initMarginChange="100", maintMarginChange="80", equityWithLoanChange="-20"
    )
    assert margin_cost(debit, "USD", 5) == (125, 105)
    with pytest.raises(BatchRiskError):
        margin_cost(OrderState(), "USD", 5)
    debit.warningText = "Insufficient funds"
    with pytest.raises(BatchRiskError):
        margin_cost(debit, "USD", 5)


@pytest.mark.asyncio
async def test_batch_margin_rejection_sends_no_real_orders(manager, broker, mocker):
    manager.completion_future = asyncio.get_running_loop().create_future()
    mocker.patch.object(manager, "initialize_account")
    mocker.patch.object(manager, "summarize_account", return_value=({}, {}))
    mocker.patch.object(manager, "options_trading_enabled", return_value=False)
    broker.accountValues.return_value = list(summary(1000, 10000).values())
    broker.whatIfOrderAsync.return_value = OrderState(
        initMarginChange="500", maintMarginChange="500", equityWithLoanChange="0"
    )
    for item in [stock_order(), stock_order("QQQ")]:
        manager.order_ops.enqueue_order(*item)
    with pytest.raises(BatchRiskError, match="margin reservations"):
        await manager.manage()
    assert isinstance(manager.completion_future.exception(), BatchRiskError)
    assert broker.whatIfOrderAsync.await_count == 2
    broker.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_existing_working_order_margin_is_reserved(manager, broker):
    contract, order = stock_order("QQQ")
    broker.reqAllOpenOrdersAsync.return_value = [
        Trade(contract, order, OrderStatus(status="Submitted", remaining=1))
    ]
    broker.accountValues.return_value = list(summary(1000, 10000).values())
    broker.whatIfOrderAsync.return_value = OrderState(
        initMarginChange="500", maintMarginChange="500", equityWithLoanChange="0"
    )
    manager.order_ops.enqueue_order(*stock_order())
    with pytest.raises(BatchRiskError, match="margin reservations"):
        await manager.preflight_orders()
    assert broker.whatIfOrderAsync.await_count == 2
    broker.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_valid_batch_is_checked_before_first_submit(manager, broker, mocker):
    manager.completion_future = asyncio.get_running_loop().create_future()
    mocker.patch.object(manager, "initialize_account")
    mocker.patch.object(manager, "summarize_account", return_value=({}, {}))
    mocker.patch.object(manager, "options_trading_enabled", return_value=False)
    mocker.patch.object(manager, "adjust_prices")
    mocker.patch.object(manager.ibkr, "wait_for_submitting_orders")
    for item in [stock_order(), stock_order("QQQ")]:
        manager.order_ops.enqueue_order(*item)

    def submit(contract, order):
        assert broker.whatIfOrderAsync.await_count == 2
        return Trade(contract, order, OrderStatus(status="Submitted"))

    broker.placeOrder.side_effect = submit
    await manager.manage()
    assert await manager.completion_future is True
    assert broker.placeOrder.call_count == 2
    assert manager.execution_result.status == "working"


@pytest.mark.asyncio
async def test_dry_run_never_sends_margin_preview(broker):
    adapter = IBKR(broker, 1, "SMART", dry_run=True)
    with pytest.raises(DryRunOrderError):
        await adapter.preview_order(*stock_order())
    broker.whatIfOrderAsync.assert_not_called()
    broker.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_repricing_risk_failure_stops_amendment(manager, broker, mocker):
    contract, order = stock_order()
    order.orderId = 10
    trade = Trade(contract, order, OrderStatus(status="Submitted"))
    broker.placeOrder.return_value = trade
    manager.trades.submit_order(contract, order)
    broker.placeOrder.reset_mock()
    mocker.patch.object(manager.ibkr, "wait_for_orders_complete")
    ticker = Mock()
    ticker.midpoint.return_value = 102
    mocker.patch.object(manager.ibkr, "get_ticker_for_contract", return_value=ticker)
    broker.accountValues.return_value = list(summary(1, 1).values())
    with pytest.raises(BatchRiskError):
        await manager.adjust_prices()
    broker.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_margin_timeout_aborts_before_any_order(manager, broker):
    manager.order_ops.enqueue_order(*stock_order())
    broker.whatIfOrderAsync.side_effect = asyncio.TimeoutError
    with pytest.raises(RuntimeError, match="margin preview"):
        await manager.preflight_orders()
    broker.placeOrder.assert_not_called()
