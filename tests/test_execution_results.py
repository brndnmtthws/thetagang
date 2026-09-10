import asyncio
from unittest.mock import Mock

import pytest
from ib_async import LimitOrder, OrderStatus, Stock, Trade
from pytest_mock import MockerFixture

from thetagang.execution import (
    ExecutionError,
    OrderSubmissionError,
    execution_result,
)
from thetagang.portfolio_manager import PortfolioManager
from thetagang.trades import Trades


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([], "success"),
        (["Filled"], "success"),
        (["Submitted", "PreSubmitted"], "working"),
        (["Filled", "Inactive"], "partial_failure"),
        (["Cancelled"], "failed"),
        (["PendingSubmit"], "unknown"),
        (["Submitted", "PendingCancel"], "unknown"),
    ],
)
def test_classifies_broker_outcomes(statuses: list[str], expected: str) -> None:
    trades = [Trade(orderStatus=OrderStatus(status=s)) for s in statuses]
    assert execution_result(trades).status == expected


def test_partially_filled_then_cancelled_order_is_partial_failure() -> None:
    trade = Trade(orderStatus=OrderStatus(status="Cancelled", filled=5, remaining=5))
    result = execution_result([trade])
    assert result.status == "partial_failure"
    assert result.partially_filled == 1
    assert result.successful is False


@pytest.mark.asyncio
@pytest.mark.parametrize("submission_failure", [False, True])
async def test_failure_reaches_completion_future(
    manager: PortfolioManager,
    broker: Mock,
    mocker: MockerFixture,
    submission_failure: bool,
) -> None:
    manager.completion_future = asyncio.get_running_loop().create_future()
    manager.data_store = mocker.Mock()
    mocker.patch.object(manager, "initialize_account")
    mocker.patch.object(manager, "options_trading_enabled", return_value=False)
    summary = mocker.patch.object(manager, "summarize_account", return_value=({}, {}))
    if submission_failure:
        manager.orders.add_order(
            Stock("SPY", "SMART", "USD"),
            LimitOrder("BUY", 1, 100, account="DU-TEST"),
            1,
        )
        broker.placeOrder.side_effect = RuntimeError("socket failure")
        expected_exception = OrderSubmissionError
    else:
        summary.side_effect = ValueError("bad account data")
        expected_exception = ValueError

    with pytest.raises(expected_exception):
        await manager.manage()
    with pytest.raises(expected_exception):
        await manager.completion_future
    payload = manager.data_store.record_event.call_args.args[1]
    assert payload["success"] is False
    assert payload["status"] == ("unknown" if submission_failure else "failed")


@pytest.mark.asyncio
async def test_existing_completion_error_is_not_overwritten(
    manager: PortfolioManager, mocker: MockerFixture
) -> None:
    future = asyncio.get_running_loop().create_future()
    original_error = RuntimeError("connection lost")
    future.set_exception(original_error)
    manager.completion_future = future
    mocker.patch.object(
        manager, "initialize_account", side_effect=ValueError("failure")
    )
    with pytest.raises(ValueError):
        await manager.manage()
    assert future.exception() is original_error


@pytest.mark.asyncio
async def test_finalization_failure_does_not_hide_original_error(manager, mocker):
    manager.completion_future = asyncio.get_running_loop().create_future()
    original = ValueError("original failure")
    mocker.patch.object(manager, "initialize_account", side_effect=original)
    manager.data_store = mocker.Mock()
    manager.data_store.record_event.side_effect = [None, OSError("log failed")]
    with pytest.raises(ValueError, match="original"):
        await manager.manage()
    assert manager.completion_future.exception() is original


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["PendingSubmit", "Inactive", "Cancelled"])
async def test_unconfirmed_or_rejected_order_fails_batch(
    manager: PortfolioManager, mocker: MockerFixture, status: str
) -> None:
    manager.completion_future = asyncio.get_running_loop().create_future()
    mocker.patch.object(manager, "initialize_account")
    mocker.patch.object(manager, "summarize_account", return_value=({}, {}))
    mocker.patch.object(manager, "options_trading_enabled", return_value=False)
    mocker.patch.object(manager, "submit_orders")
    mocker.patch.object(manager, "adjust_prices")
    mocker.patch.object(manager.ibkr, "wait_for_submitting_orders")
    mocker.patch.object(
        manager.trades,
        "records",
        return_value=[Trade(orderStatus=OrderStatus(status=status))],
    )
    with pytest.raises(ExecutionError):
        await manager.manage()
    with pytest.raises(ExecutionError):
        await manager.completion_future


def test_amendments_keep_order_intent_association(mocker: MockerFixture) -> None:
    adapter, store = mocker.Mock(), mocker.Mock()
    trades = Trades(adapter, store)
    contract = Stock("SPY", "SMART", "USD")
    order = LimitOrder("BUY", 1, 100)
    trades.submit_order(contract, order, intent_id=71)
    amended = LimitOrder("BUY", 1, 101)
    trades.submit_order(contract, amended, idx=0)
    store.record_order.assert_called_with(contract, amended, intent_id=71)


@pytest.mark.asyncio
async def test_skipped_repricing_does_not_skip_later_orders(
    manager: PortfolioManager, broker: Mock, mocker: MockerFixture
) -> None:
    contract = Stock("SPY", "SMART", "USD")
    for i, action in enumerate(["SELL", "BUY"]):
        order = LimitOrder(action, 1, 100, orderId=i + 1, account="DU-TEST")
        broker.placeOrder.return_value = Trade(
            contract, order, OrderStatus(status="Submitted")
        )
        manager.trades.submit_order(contract, order, intent_id=i + 1)
    broker.placeOrder.reset_mock()
    mocker.patch.object(manager.ibkr, "wait_for_orders_complete")
    ticker = mocker.Mock()
    ticker.midpoint.return_value = 102
    mocker.patch.object(manager.ibkr, "get_ticker_for_contract", return_value=ticker)
    await manager.adjust_prices()
    broker.placeOrder.assert_called_once()
    assert broker.placeOrder.call_args.args[1].orderId == 2
    assert broker.placeOrder.call_args.args[1].lmtPrice == 101
