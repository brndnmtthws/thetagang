import time
from types import SimpleNamespace

import pytest
from ib_async import IB, Contract, LimitOrder, Option, OrderStatus, Trade

from thetagang.config import Config
from thetagang.ibkr import IBKR
from thetagang.order_execution import OrderExecutionManager
from thetagang.strategies.tail_hedge_state import TAIL_HEDGE_ENTRY_ORDER_REF
from thetagang.trades import Trades


@pytest.fixture(autouse=True)
def _no_order_error_grace(monkeypatch):
    """Keep unit tests fast; the rejection-reason grace is covered explicitly."""
    monkeypatch.setattr("thetagang.order_execution.ORDER_ERROR_GRACE_SECONDS", 0.0)


def _config(*, execution=None, minimum_credit=0.05) -> Config:
    symbol = {"weight": 1.0}
    if execution is not None:
        symbol["execution"] = execution
    return Config(
        meta={"schema_version": 2},
        run={"strategies": ["wheel"]},
        runtime={
            "account": {"number": "DUX", "margin_usage": 0.5},
            "option_chains": {"expirations": 4, "strikes": 10},
            "orders": {
                "minimum_credit": minimum_credit,
                "price_update_delay": [1, 2],
            },
            "ib_async": {"api_response_wait_time": 1},
        },
        portfolio={"symbols": {"AAA": symbol}},
        strategies={
            "wheel": {
                "defaults": {
                    "target": {"dte": 30, "minimum_open_interest": 5},
                    "roll_when": {"dte": 7},
                }
            }
        },
    )


def _option() -> Option:
    return Option(
        "AAA",
        "20270115",
        100,
        "C",
        "SMART",
        currency="USD",
        conId=123,
    )


def _ticker(contract: Contract, *, bid=0.9, ask=1.1, mid=1.0):
    return SimpleNamespace(
        contract=contract,
        bid=bid,
        ask=ask,
        midpoint=lambda: mid,
    )


def _real_ib_with_pending_trade(order_id: int = 5) -> tuple[IB, Trade]:
    """Build an unconnected IB whose wrapper holds one PendingSubmit trade.

    The trade is seeded exactly as ib_async keeps it, so driving
    ``wrapper.error()`` reproduces the synchronous status mutation, log entry,
    and event emissions the supervisor competes with in production.
    """
    ib = IB()
    order = LimitOrder("BUY", 18, 20.38, account="DUX", orderRef="tg:test")
    order.orderId = order_id
    trade = Trade(
        contract=_option(),
        order=order,
        orderStatus=OrderStatus(orderId=order_id, status="PendingSubmit"),
    )
    ib.wrapper.trades[(ib.wrapper.clientId, order_id)] = trade
    return ib, trade


@pytest.mark.asyncio
async def test_prepare_orders_applies_per_side_price_strategy(mocker) -> None:
    config = _config(execution={"buy_price": "ask", "sell_price": "bid"})
    contract = _option()
    ibkr = mocker.Mock()
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, bid=0.43, ask=0.57)
    )
    manager = OrderExecutionManager(config, ibkr)
    buy = LimitOrder("BUY", 1, 1.0, account="DUX")
    sell = LimitOrder("SELL", 1, 1.0, account="DUX")

    await manager.prepare_orders([(contract, buy, None), (contract, sell, None)])

    assert buy.lmtPrice == pytest.approx(0.57)
    assert sell.lmtPrice == pytest.approx(0.43)


@pytest.mark.asyncio
async def test_prepare_orders_applies_mid_price_strategy(mocker) -> None:
    config = _config(execution={"buy_price": "mid"})
    contract = _option()
    ibkr = mocker.Mock()
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, bid=0.43, ask=0.57, mid=0.51)
    )
    manager = OrderExecutionManager(config, ibkr)
    order = LimitOrder("BUY", 1, 1.0, account="DUX")

    await manager.prepare_orders([(contract, order, None)])

    assert order.lmtPrice == pytest.approx(0.51)


@pytest.mark.asyncio
async def test_prepare_orders_preserves_fallback_for_zero_quote(mocker) -> None:
    config = _config(execution={"sell_price": "bid"})
    contract = _option()
    ibkr = mocker.Mock()
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, bid=0.0)
    )
    manager = OrderExecutionManager(config, ibkr)
    order = LimitOrder("SELL", 1, 0.75, account="DUX")

    await manager.prepare_orders([(contract, order, None)])

    assert order.lmtPrice == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_prepare_orders_preserves_minimum_credit(mocker) -> None:
    config = _config(execution={"sell_price": "bid"}, minimum_credit=0.05)
    contract = _option()
    ibkr = mocker.Mock()
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, bid=0.01)
    )
    manager = OrderExecutionManager(config, ibkr)
    order = LimitOrder("SELL", 1, 0.08, account="DUX")

    await manager.prepare_orders([(contract, order, None)])

    assert order.lmtPrice == pytest.approx(0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ask", "expected_price"),
    [(-0.01, -0.05), (0.10, -0.50)],
)
async def test_prepare_orders_preserves_combo_credit_safeguards(
    mocker,
    ask,
    expected_price,
) -> None:
    config = _config(execution={"buy_price": "ask"}, minimum_credit=0.05)
    contract = Contract(
        secType="BAG",
        symbol="AAA",
        exchange="SMART",
        currency="USD",
    )
    ibkr = mocker.Mock()
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, ask=ask)
    )
    manager = OrderExecutionManager(config, ibkr)
    order = LimitOrder("BUY", 1, -0.50, account="DUX")

    await manager.prepare_orders([(contract, order, None)])

    assert order.lmtPrice == pytest.approx(expected_price)


@pytest.mark.asyncio
async def test_prepare_orders_leaves_tail_orders_on_specialized_path(mocker) -> None:
    config = _config(execution={"buy_price": "ask"})
    contract = _option()
    ibkr = mocker.Mock()
    ibkr.get_ticker_for_contract = mocker.AsyncMock()
    manager = OrderExecutionManager(config, ibkr)
    order = LimitOrder(
        "BUY",
        1,
        0.75,
        account="DUX",
        orderRef=TAIL_HEDGE_ENTRY_ORDER_REF,
    )

    await manager.prepare_orders([(contract, order, None)])

    assert order.lmtPrice == pytest.approx(0.75)
    ibkr.get_ticker_for_contract.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfigured_execution_does_not_wait_or_cancel(mocker) -> None:
    config = _config()
    contract = _option()
    order = LimitOrder("SELL", 1, 0.5, account="DUX")
    trade = mocker.Mock(
        contract=contract,
        order=order,
        orderStatus=SimpleNamespace(status="Submitted", filled=0.0, remaining=1.0),
    )
    trade.isDone.return_value = False
    ibkr = mocker.Mock()
    ibkr.get_ticker_for_contract = mocker.AsyncMock()
    ibkr.wait_for_orders_complete = mocker.AsyncMock()
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    trades.is_empty.return_value = False
    manager = OrderExecutionManager(config, ibkr)

    await manager.prepare_orders([(contract, order, None)])
    await manager.execute(trades)

    assert order.lmtPrice == pytest.approx(0.5)
    ibkr.get_ticker_for_contract.assert_not_awaited()
    ibkr.wait_for_orders_complete.assert_not_awaited()
    ibkr.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_leave_open_timeout_preserves_working_order(mocker) -> None:
    config = _config(execution={"fill_timeout": 1})
    contract = _option()
    order = LimitOrder("SELL", 1, 0.5, account="DUX")
    trade = mocker.Mock(
        contract=contract,
        order=order,
        orderStatus=SimpleNamespace(status="Submitted", filled=0.0, remaining=1.0),
    )
    trade.isDone.return_value = False
    ibkr = mocker.Mock()
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[trade])
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    trades.is_empty.return_value = False
    manager = OrderExecutionManager(config, ibkr)

    await manager.execute(trades)

    ibkr.cancel_order.assert_not_called()
    trades.submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_fill_timeout_includes_time_spent_repricing(mocker) -> None:
    config = _config(
        execution={"buy_price": "ask", "fill_timeout": 10},
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    order = LimitOrder("BUY", 1, 0.5, account="DUX")
    trade = mocker.Mock(
        contract=contract,
        order=order,
        orderStatus=SimpleNamespace(status="Submitted", filled=0.0, remaining=1.0),
    )
    trade.isDone.return_value = False
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    elapsed = 0.0

    async def wait_for_orders_complete(_trades, timeout):
        nonlocal elapsed
        elapsed += timeout
        return [trade]

    async def reprice_trade(*_args, **_kwargs):
        nonlocal elapsed
        elapsed += 3.0
        return True

    ibkr = mocker.Mock()
    ibkr.wait_for_orders_complete = wait_for_orders_complete
    manager = OrderExecutionManager(config, ibkr)
    mocker.patch.object(manager, "reprice_trade", side_effect=reprice_trade)
    manager._handle_timeout = mocker.AsyncMock()
    mocker.patch(
        "thetagang.order_execution.asyncio.get_running_loop",
        return_value=SimpleNamespace(time=lambda: elapsed),
    )
    mocker.patch("thetagang.order_execution.random.randrange", return_value=6)

    await manager._supervise_trade(trades, 0, trade, policy)

    assert elapsed == pytest.approx(10.0)
    manager._handle_timeout.assert_awaited_once_with(trades, 0, policy)


@pytest.mark.asyncio
async def test_marketable_limit_replaces_only_partially_filled_remainder(
    mocker,
) -> None:
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    original_order = LimitOrder(
        "SELL",
        2,
        0.5,
        account="DUX",
        orderRef="tg:test",
    )
    original_status = SimpleNamespace(status="Submitted", filled=1.0, remaining=1.0)
    original_trade = mocker.Mock(
        contract=contract,
        order=original_order,
        orderStatus=original_status,
    )
    original_trade.isDone.side_effect = lambda: original_status.status == "Cancelled"
    records = [original_trade]
    ibkr = mocker.Mock()

    def cancel_order(_order):
        original_status.status = "Cancelled"

    ibkr.cancel_order.side_effect = cancel_order
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[])
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, bid=0.42)
    )
    trades = mocker.Mock(spec=Trades)
    trades.records.side_effect = lambda: records

    def submit_order(submitted_contract, submitted_order, idx):
        replacement_status = SimpleNamespace(
            status="Filled",
            filled=1.0,
            remaining=0.0,
        )
        replacement_trade = mocker.Mock(
            contract=submitted_contract,
            order=submitted_order,
            orderStatus=replacement_status,
        )
        replacement_trade.isDone.return_value = True
        records[idx] = replacement_trade
        return True

    trades.submit_order.side_effect = submit_order
    manager = OrderExecutionManager(config, ibkr)

    await manager._handle_timeout(trades, 0, policy)

    submitted_order = trades.submit_order.call_args.args[1]
    assert submitted_order.orderType == "LMT"
    assert submitted_order.totalQuantity == 1
    assert submitted_order.lmtPrice == pytest.approx(0.42)
    assert submitted_order.orderRef == "tg:test"
    assert submitted_order.algoStrategy == ""


@pytest.mark.asyncio
async def test_marketable_limit_does_not_flip_combo_credit_to_debit(mocker) -> None:
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = Contract(
        secType="BAG",
        symbol="AAA",
        exchange="SMART",
        currency="USD",
    )
    order = LimitOrder("BUY", 1, -0.5, account="DUX")
    status = SimpleNamespace(status="Submitted", filled=0.0, remaining=1.0)
    trade = mocker.Mock(contract=contract, order=order, orderStatus=status)
    trade.isDone.side_effect = lambda: status.status == "Cancelled"
    ibkr = mocker.Mock()

    def cancel_order(_order):
        status.status = "Cancelled"

    ibkr.cancel_order.side_effect = cancel_order
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[])
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, ask=0.10)
    )
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    manager = OrderExecutionManager(config, ibkr)

    await manager._handle_timeout(trades, 0, policy)

    ibkr.cancel_order.assert_called_once_with(order)
    trades.submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_timeout_does_not_replace_without_confirmed_cancellation(mocker) -> None:
    config = _config(
        execution={"fill_timeout": 300, "on_timeout": "market", "final_wait": 1}
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    order = LimitOrder("BUY", 1, 0.5, account="DUX")
    trade = mocker.Mock(
        contract=contract,
        order=order,
        orderStatus=SimpleNamespace(status="Submitted", filled=0.0, remaining=1.0),
    )
    trade.isDone.return_value = False
    ibkr = mocker.Mock()
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[trade])
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    manager = OrderExecutionManager(config, ibkr)

    await manager._handle_timeout(trades, 0, policy)

    ibkr.cancel_order.assert_called_once_with(order)
    trades.submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_market_timeout_replaces_only_confirmed_remainder(mocker) -> None:
    config = _config(
        execution={"fill_timeout": 300, "on_timeout": "market", "final_wait": 1}
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    original_order = LimitOrder("BUY", 3, 0.5, account="DUX", orderRef="tg:test")
    original_status = SimpleNamespace(status="Submitted", filled=1.0, remaining=2.0)
    original_trade = mocker.Mock(
        contract=contract,
        order=original_order,
        orderStatus=original_status,
    )
    original_trade.isDone.side_effect = lambda: original_status.status == "Cancelled"
    records = [original_trade]
    ibkr = mocker.Mock()

    def cancel_order(_order):
        original_status.status = "Cancelled"

    ibkr.cancel_order.side_effect = cancel_order
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[])
    trades = mocker.Mock(spec=Trades)
    trades.records.side_effect = lambda: records

    def submit_order(submitted_contract, submitted_order, idx):
        replacement_status = SimpleNamespace(
            status="Filled",
            filled=2.0,
            remaining=0.0,
        )
        replacement_trade = mocker.Mock(
            contract=submitted_contract,
            order=submitted_order,
            orderStatus=replacement_status,
        )
        replacement_trade.isDone.return_value = True
        records[idx] = replacement_trade
        return True

    trades.submit_order.side_effect = submit_order
    manager = OrderExecutionManager(config, ibkr)

    await manager._handle_timeout(trades, 0, policy)

    submitted_order = trades.submit_order.call_args.args[1]
    assert submitted_order.orderType == "MKT"
    assert submitted_order.totalQuantity == 2
    assert submitted_order.orderRef == "tg:test"
    assert submitted_order.algoStrategy == ""


@pytest.mark.asyncio
async def test_market_timeout_cancels_combo_without_replacement(mocker) -> None:
    config = _config(
        execution={"fill_timeout": 300, "on_timeout": "market", "final_wait": 1}
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = Contract(secType="BAG", symbol="AAA", exchange="SMART", currency="USD")
    order = LimitOrder("BUY", 1, -0.5, account="DUX")
    status = SimpleNamespace(status="Submitted", filled=0.0, remaining=1.0)
    trade = mocker.Mock(contract=contract, order=order, orderStatus=status)
    trade.isDone.side_effect = lambda: status.status == "Cancelled"
    ibkr = mocker.Mock()

    def cancel_order(_order):
        status.status = "Cancelled"

    ibkr.cancel_order.side_effect = cancel_order
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[])
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    manager = OrderExecutionManager(config, ibkr)

    await manager._handle_timeout(trades, 0, policy)

    ibkr.cancel_order.assert_called_once_with(order)
    trades.submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_inactive_order_replaces_full_quantity(mocker) -> None:
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    original_order = LimitOrder("BUY", 18, 20.38, account="DUX", orderRef="tg:test")
    original_status = SimpleNamespace(status="Submitted", filled=0.0, remaining=18.0)
    original_trade = mocker.Mock(
        contract=contract,
        order=original_order,
        orderStatus=original_status,
    )
    original_trade.isDone.side_effect = lambda: original_status.status == "Inactive"
    records = [original_trade]
    ibkr = mocker.Mock()
    ibkr.order_error.return_value = None

    async def wait_for_orders_complete(_trades, _timeout):
        original_status.status = "Inactive"
        return []

    ibkr.wait_for_orders_complete = wait_for_orders_complete
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, ask=20.40)
    )
    trades = mocker.Mock(spec=Trades)
    trades.records.side_effect = lambda: records

    def submit_order(submitted_contract, submitted_order, idx):
        replacement_status = SimpleNamespace(
            status="Filled",
            filled=18.0,
            remaining=0.0,
        )
        replacement_trade = mocker.Mock(
            contract=submitted_contract,
            order=submitted_order,
            orderStatus=replacement_status,
        )
        replacement_trade.isDone.return_value = True
        records[idx] = replacement_trade
        return True

    trades.submit_order.side_effect = submit_order
    manager = OrderExecutionManager(config, ibkr)

    await manager._supervise_trade(trades, 0, original_trade, policy)

    assert trades.submit_order.call_count == 1
    submitted_order = trades.submit_order.call_args.args[1]
    assert submitted_order.orderType == "LMT"
    assert submitted_order.action == "BUY"
    assert submitted_order.totalQuantity == 18
    assert submitted_order.lmtPrice == pytest.approx(20.40)
    assert submitted_order.orderRef == "tg:test"
    ibkr.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_inactive_order_replaces_only_partial_remainder(mocker) -> None:
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    original_order = LimitOrder("BUY", 18, 20.38, account="DUX", orderRef="tg:test")
    original_status = SimpleNamespace(status="Inactive", filled=5.0, remaining=13.0)
    original_trade = mocker.Mock(
        contract=contract,
        order=original_order,
        orderStatus=original_status,
    )
    original_trade.isDone.return_value = True
    records = [original_trade]
    ibkr = mocker.Mock()
    ibkr.order_error.return_value = None
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[])
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, ask=20.40)
    )
    trades = mocker.Mock(spec=Trades)
    trades.records.side_effect = lambda: records

    def submit_order(submitted_contract, submitted_order, idx):
        replacement_status = SimpleNamespace(
            status="Filled",
            filled=13.0,
            remaining=0.0,
        )
        replacement_trade = mocker.Mock(
            contract=submitted_contract,
            order=submitted_order,
            orderStatus=replacement_status,
        )
        replacement_trade.isDone.return_value = True
        records[idx] = replacement_trade
        return True

    trades.submit_order.side_effect = submit_order
    manager = OrderExecutionManager(config, ibkr)

    await manager._handle_rejected(trades, 0, policy)

    assert trades.submit_order.call_count == 1
    submitted_order = trades.submit_order.call_args.args[1]
    assert submitted_order.orderType == "LMT"
    assert submitted_order.totalQuantity == 13
    assert submitted_order.lmtPrice == pytest.approx(20.40)
    ibkr.cancel_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["Cancelled", "ApiCancelled"])
async def test_cancelled_without_rejection_reason_does_not_replace(
    mocker, status
) -> None:
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    order = LimitOrder("BUY", 1, 20.38, account="DUX")
    trade = mocker.Mock(
        contract=contract,
        order=order,
        orderStatus=SimpleNamespace(status=status, filled=0.0, remaining=1.0),
    )
    trade.isDone.return_value = True
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    ibkr = mocker.Mock()
    ibkr.order_error.return_value = None
    manager = OrderExecutionManager(config, ibkr)
    manager._handle_rejected = mocker.AsyncMock()

    await manager._supervise_trade(trades, 0, trade, policy)

    manager._handle_rejected.assert_not_awaited()
    trades.submit_order.assert_not_called()
    ibkr.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_rejected_replacement_stops_without_retry(mocker, capsys) -> None:
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    original_order = LimitOrder("BUY", 18, 20.38, account="DUX")
    original_status = SimpleNamespace(status="Inactive", filled=0.0, remaining=18.0)
    original_trade = mocker.Mock(
        contract=contract,
        order=original_order,
        orderStatus=original_status,
    )
    original_trade.isDone.return_value = True
    records = [original_trade]
    ibkr = mocker.Mock()
    ibkr.order_error.return_value = None
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[])
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, ask=20.40)
    )
    trades = mocker.Mock(spec=Trades)
    trades.records.side_effect = lambda: records

    def submit_order(submitted_contract, submitted_order, idx):
        replacement_status = SimpleNamespace(
            status="Inactive",
            filled=0.0,
            remaining=13.0,
            whyHeld="broker risk check",
        )
        replacement_trade = mocker.Mock(
            contract=submitted_contract,
            order=submitted_order,
            orderStatus=replacement_status,
        )
        replacement_trade.isDone.return_value = True
        records[idx] = replacement_trade
        return True

    trades.submit_order.side_effect = submit_order
    data_store = mocker.Mock()
    manager = OrderExecutionManager(config, ibkr, data_store=data_store)

    await manager._handle_rejected(trades, 0, policy)

    assert trades.submit_order.call_count == 1
    ibkr.cancel_order.assert_not_called()
    assert "Rejected replacement was rejected by the broker" in capsys.readouterr().out
    replacement_events = [
        call
        for call in data_store.record_event.call_args_list
        if call.args[0] == "order_replacement_rejected"
    ]
    assert len(replacement_events) == 1
    payload = replacement_events[0].args[1]
    assert payload == {
        "symbol": "AAA",
        "order_id": 0,
        "action": "BUY",
        "status": "Inactive",
        "error_code": None,
        "error_message": None,
        "why_held": "broker risk check",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "execution",
    [
        {"fill_timeout": 300},
        {
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "on_inactive": "leave_open",
        },
    ],
)
async def test_inactive_left_unreplaced_when_policy_leaves_it(
    mocker, execution
) -> None:
    config = _config(execution=execution)
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    order = LimitOrder("BUY", 1, 20.38, account="DUX")
    trade = mocker.Mock(
        contract=contract,
        order=order,
        orderStatus=SimpleNamespace(status="Inactive", filled=0.0, remaining=1.0),
    )
    trade.isDone.return_value = True
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    ibkr = mocker.Mock()
    ibkr.order_error.return_value = None
    manager = OrderExecutionManager(config, ibkr)

    await manager._handle_rejected(trades, 0, policy)

    trades.submit_order.assert_not_called()
    ibkr.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_on_inactive_overrides_on_timeout(mocker) -> None:
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "leave_open",
            "on_inactive": "market",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    original_order = LimitOrder("BUY", 18, 20.38, account="DUX", orderRef="tg:test")
    original_status = SimpleNamespace(status="Inactive", filled=0.0, remaining=18.0)
    original_trade = mocker.Mock(
        contract=contract,
        order=original_order,
        orderStatus=original_status,
    )
    original_trade.isDone.return_value = True
    records = [original_trade]
    ibkr = mocker.Mock()
    ibkr.order_error.return_value = None
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[])
    trades = mocker.Mock(spec=Trades)
    trades.records.side_effect = lambda: records

    def submit_order(submitted_contract, submitted_order, idx):
        replacement_status = SimpleNamespace(
            status="Filled",
            filled=18.0,
            remaining=0.0,
        )
        replacement_trade = mocker.Mock(
            contract=submitted_contract,
            order=submitted_order,
            orderStatus=replacement_status,
        )
        replacement_trade.isDone.return_value = True
        records[idx] = replacement_trade
        return True

    trades.submit_order.side_effect = submit_order
    manager = OrderExecutionManager(config, ibkr)

    await manager._handle_rejected(trades, 0, policy)

    submitted_order = trades.submit_order.call_args.args[1]
    assert submitted_order.orderType == "MKT"
    assert submitted_order.totalQuantity == 18
    assert submitted_order.orderRef == "tg:test"


@pytest.mark.asyncio
async def test_inactive_reports_broker_reason_and_records_event(mocker) -> None:
    config = _config(
        execution={"fill_timeout": 300, "on_timeout": "cancel", "final_wait": 1}
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    order = LimitOrder("BUY", 1, 20.38, account="DUX")
    order.orderId = 42
    trade = mocker.Mock(
        contract=contract,
        order=order,
        orderStatus=SimpleNamespace(
            status="Inactive",
            filled=0.0,
            remaining=1.0,
            whyHeld="insufficient buying power",
        ),
    )
    trade.isDone.return_value = True
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    ibkr = mocker.Mock()
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[])
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, ask=20.40)
    )
    ibkr.order_error.return_value = (
        431,
        "The order wouldn't conform to the margin requirements",
    )
    data_store = mocker.Mock()
    manager = OrderExecutionManager(config, ibkr, data_store=data_store)

    await manager._handle_rejected(trades, 0, policy)

    ibkr.order_error.assert_called_once_with(42)
    data_store.record_event.assert_called_once_with(
        "order_broker_rejected",
        {
            "symbol": "AAA",
            "order_id": 42,
            "action": "BUY",
            "status": "Inactive",
            "rejection_action": "cancel",
            "remaining": 1.0,
            "error_code": 431,
            "error_message": "The order wouldn't conform to the margin requirements",
            "why_held": "insufficient buying power",
        },
    )


@pytest.mark.asyncio
async def test_inactive_with_invalid_fill_quantities_does_not_replace(mocker) -> None:
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    order = LimitOrder("BUY", 3, 20.38, account="DUX")
    trade = mocker.Mock(
        contract=contract,
        order=order,
        orderStatus=SimpleNamespace(status="Inactive", filled=5.0, remaining=0.0),
    )
    trade.isDone.return_value = True
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    ibkr = mocker.Mock()
    ibkr.order_error.return_value = None
    data_store = mocker.Mock()
    manager = OrderExecutionManager(config, ibkr, data_store=data_store)

    await manager._handle_rejected(trades, 0, policy)

    trades.submit_order.assert_not_called()
    payload = data_store.record_event.call_args.args[1]
    assert payload["remaining"] is None


@pytest.mark.asyncio
async def test_inactive_with_complete_fill_does_not_replace(mocker) -> None:
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    order = LimitOrder("BUY", 18, 20.38, account="DUX")
    trade = mocker.Mock(
        contract=contract,
        order=order,
        orderStatus=SimpleNamespace(status="Inactive", filled=18.0, remaining=0.0),
    )
    trade.isDone.return_value = True
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    ibkr = mocker.Mock()
    ibkr.order_error.return_value = None
    manager = OrderExecutionManager(config, ibkr)

    await manager._handle_rejected(trades, 0, policy)

    trades.submit_order.assert_not_called()
    ibkr.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_error_cancelled_rejection_replaces_once(mocker) -> None:
    ib, trade = _real_ib_with_pending_trade()
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    data_store = mocker.Mock()
    ibkr = IBKR(
        ib=ib,
        api_response_wait_time=1,
        default_order_exchange="SMART",
        data_store=data_store,
    )
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(trade.contract, ask=20.40)
    )
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[])
    ibkr.cancel_order = mocker.Mock()

    # ib_async's wrapper synchronously marks a rejected unfinished order
    # Cancelled and then emits the reason on errorEvent, in one callback.
    ib.wrapper.error(5, 201, "Order rejected - reason: insufficient margin", "")

    assert trade.orderStatus.status == "Cancelled"
    assert ibkr.order_error(5) == (
        201,
        "Order rejected - reason: insufficient margin",
    )

    records = [trade]
    trades = mocker.Mock(spec=Trades)
    trades.records.side_effect = lambda: records

    def submit_order(submitted_contract, submitted_order, idx):
        records[idx] = Trade(
            contract=submitted_contract,
            order=submitted_order,
            orderStatus=OrderStatus(status="Filled", filled=18.0, remaining=0.0),
        )
        return True

    trades.submit_order.side_effect = submit_order
    manager = OrderExecutionManager(config, ibkr, data_store=data_store)

    await manager._handle_terminal_state(trades, 0, policy, trade)

    assert trades.submit_order.call_count == 1
    submitted_order = trades.submit_order.call_args.args[1]
    assert submitted_order.orderType == "LMT"
    assert submitted_order.totalQuantity == 18
    ibkr.cancel_order.assert_not_called()
    rejected_events = [
        call
        for call in data_store.record_event.call_args_list
        if call.args[0] == "order_broker_rejected"
    ]
    assert len(rejected_events) == 1
    payload = rejected_events[0].args[1]
    assert payload["status"] == "Cancelled"
    assert payload["error_code"] == 201
    assert payload["remaining"] == 18.0
    assert payload["rejection_action"] == "marketable_limit"


@pytest.mark.asyncio
async def test_error_cancelled_external_cancel_does_not_replace(mocker, capsys) -> None:
    ib, trade = _real_ib_with_pending_trade()
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    data_store = mocker.Mock()
    ibkr = IBKR(
        ib=ib,
        api_response_wait_time=1,
        default_order_exchange="SMART",
        data_store=data_store,
    )
    ibkr.cancel_order = mocker.Mock()
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    manager = OrderExecutionManager(config, ibkr)

    ib.wrapper.error(5, 202, "Order Canceled - Reason: OCA canceled", "")

    assert trade.orderStatus.status == "Cancelled"

    await manager._handle_terminal_state(trades, 0, policy, trade)

    trades.submit_order.assert_not_called()
    ibkr.cancel_order.assert_not_called()
    assert "canceled by the broker" in capsys.readouterr().out
    assert any(
        call.args[0] == "order_error" and call.args[1]["code"] == 202
        for call in data_store.record_event.call_args_list
    )


@pytest.mark.asyncio
async def test_error_cancelled_unclassified_error_does_not_replace(
    mocker, capsys
) -> None:
    ib, trade = _real_ib_with_pending_trade()
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    ibkr = IBKR(
        ib=ib,
        api_response_wait_time=1,
        default_order_exchange="SMART",
    )
    ibkr.cancel_order = mocker.Mock()
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    manager = OrderExecutionManager(config, ibkr)

    ib.wrapper.error(
        5,
        161,
        "Cancel attempted when order is not in a cancellable state",
        "",
    )

    assert trade.orderStatus.status == "Cancelled"

    await manager._handle_terminal_state(trades, 0, policy, trade)

    trades.submit_order.assert_not_called()
    ibkr.cancel_order.assert_not_called()
    assert "unclassified broker error" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_error_cancelled_rejected_replacement_stops(mocker, capsys) -> None:
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    original_order = LimitOrder("BUY", 18, 20.38, account="DUX")
    original_trade = mocker.Mock(
        contract=contract,
        order=original_order,
        orderStatus=SimpleNamespace(status="Inactive", filled=0.0, remaining=18.0),
    )
    original_trade.isDone.return_value = True
    records = [original_trade]
    ibkr = mocker.Mock()
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[])
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, ask=20.40)
    )

    def order_error(order_id):
        if order_id == 7:
            return (201, "Order rejected - reason: insufficient margin")
        return None

    ibkr.order_error.side_effect = order_error
    trades = mocker.Mock(spec=Trades)
    trades.records.side_effect = lambda: records

    def submit_order(submitted_contract, submitted_order, idx):
        submitted_order.orderId = 7
        replacement_trade = mocker.Mock(
            contract=submitted_contract,
            order=submitted_order,
            orderStatus=SimpleNamespace(status="Cancelled", filled=0.0, remaining=18.0),
        )
        replacement_trade.isDone.return_value = True
        records[idx] = replacement_trade
        return True

    trades.submit_order.side_effect = submit_order
    data_store = mocker.Mock()
    manager = OrderExecutionManager(config, ibkr, data_store=data_store)

    await manager._handle_rejected(trades, 0, policy)

    assert trades.submit_order.call_count == 1
    ibkr.cancel_order.assert_not_called()
    captured = capsys.readouterr().out
    assert "Rejected replacement was rejected by the broker" in captured
    assert "broker error 201" in captured
    replacement_events = [
        call
        for call in data_store.record_event.call_args_list
        if call.args[0] == "order_replacement_rejected"
    ]
    assert len(replacement_events) == 1
    payload = replacement_events[0].args[1]
    assert payload["status"] == "Cancelled"
    assert payload["error_code"] == 201


@pytest.mark.asyncio
async def test_rejection_reason_arrives_within_grace(mocker, monkeypatch) -> None:
    monkeypatch.setattr("thetagang.order_execution.ORDER_ERROR_GRACE_SECONDS", 1.0)
    config = _config(
        execution={
            "fill_timeout": 300,
            "on_timeout": "marketable_limit",
            "final_wait": 1,
        }
    )
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    order = LimitOrder("BUY", 1, 20.38, account="DUX")
    trade = mocker.Mock(
        contract=contract,
        order=order,
        orderStatus=SimpleNamespace(status="Inactive", filled=0.0, remaining=1.0),
    )
    trade.isDone.return_value = True
    records = [trade]
    trades = mocker.Mock(spec=Trades)
    trades.records.side_effect = lambda: records
    ibkr = mocker.Mock()
    ibkr.get_ticker_for_contract = mocker.AsyncMock(
        return_value=_ticker(contract, ask=20.40)
    )
    ibkr.wait_for_orders_complete = mocker.AsyncMock(return_value=[])
    start = time.monotonic()

    def order_error(_order_id):
        if time.monotonic() - start >= 0.05:
            return (201, "Order rejected - reason: insufficient margin")
        return None

    ibkr.order_error.side_effect = order_error

    def submit_order(submitted_contract, submitted_order, idx):
        replacement_trade = mocker.Mock(
            contract=submitted_contract,
            order=submitted_order,
            orderStatus=SimpleNamespace(status="Filled", filled=1.0, remaining=0.0),
        )
        replacement_trade.isDone.return_value = True
        records[idx] = replacement_trade
        return True

    trades.submit_order.side_effect = submit_order
    data_store = mocker.Mock()
    manager = OrderExecutionManager(config, ibkr, data_store=data_store)

    await manager._handle_rejected(trades, 0, policy)

    rejected_events = [
        call
        for call in data_store.record_event.call_args_list
        if call.args[0] == "order_broker_rejected"
    ]
    assert len(rejected_events) == 1
    payload = rejected_events[0].args[1]
    assert payload["error_code"] == 201
    assert payload["error_message"] == "Order rejected - reason: insufficient margin"
    assert trades.submit_order.call_count == 1


@pytest.mark.asyncio
async def test_no_grace_wait_when_no_replacement_will_be_submitted(
    mocker, monkeypatch
) -> None:
    """leave_open/cancel actions must not hold the run for a late reason."""
    monkeypatch.setattr("thetagang.order_execution.ORDER_ERROR_GRACE_SECONDS", 5.0)
    config = _config(execution={"fill_timeout": 300, "on_timeout": "cancel"})
    policy = config.portfolio.symbols["AAA"].execution
    assert policy is not None
    contract = _option()
    order = LimitOrder("BUY", 1, 20.38, account="DUX")
    trade = mocker.Mock(
        contract=contract,
        order=order,
        orderStatus=SimpleNamespace(status="Inactive", filled=0.0, remaining=1.0),
    )
    trade.isDone.return_value = True
    trades = mocker.Mock(spec=Trades)
    trades.records.return_value = [trade]
    ibkr = mocker.Mock()
    ibkr.order_error.return_value = None
    data_store = mocker.Mock()
    manager = OrderExecutionManager(config, ibkr, data_store=data_store)

    started = time.monotonic()
    await manager._handle_rejected(trades, 0, policy)
    assert time.monotonic() - started < 1.0
    ibkr.order_error.assert_called_once()
    trades.submit_order.assert_not_called()
