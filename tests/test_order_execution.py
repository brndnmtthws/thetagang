from types import SimpleNamespace

import pytest
from ib_async import Contract, LimitOrder, Option

from thetagang.config import Config
from thetagang.order_execution import OrderExecutionManager
from thetagang.strategies.tail_hedge_state import TAIL_HEDGE_ENTRY_ORDER_REF
from thetagang.trades import Trades


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
