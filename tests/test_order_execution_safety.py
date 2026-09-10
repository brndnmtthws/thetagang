import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from typing import Literal
from unittest.mock import Mock

import pytest
from ib_async import LimitOrder, OrderStatus, Stock, Trade
from pytest_mock import MockerFixture

from thetagang.config import Config
from thetagang.ibkr import IBKR, DryRunOrderError
from thetagang.orders import DEFAULT_ORDER_REF
from thetagang.portfolio_manager import PortfolioManager


@pytest.mark.parametrize("mutation", ["submit", "modify", "cancel"])
def test_dry_run_blocks_broker_mutations(
    broker: Mock, mutation: Literal["submit", "modify", "cancel"]
) -> None:
    adapter = IBKR(broker, 1, "SMART", dry_run=True)
    order = LimitOrder("BUY", 1, 100, orderId=42 if mutation == "modify" else 0)

    with pytest.raises(DryRunOrderError, match="dry-run"):
        if mutation == "cancel":
            adapter.cancel_order(order)
        else:
            adapter.place_order(Stock("SPY", "SMART", "USD"), order)

    broker.placeOrder.assert_not_called()
    broker.cancelOrder.assert_not_called()


@pytest.mark.parametrize("mutation", ["submit", "modify", "cancel"])
def test_live_broker_mutations_still_work(
    broker: Mock, mutation: Literal["submit", "modify", "cancel"]
) -> None:
    adapter = IBKR(broker, 1, "SMART")
    contract = Stock("SPY", "SMART", "USD")
    order = LimitOrder("BUY", 1, 100, orderId=42 if mutation == "modify" else 0)
    if mutation == "cancel":
        adapter.cancel_order(order)
        broker.cancelOrder.assert_called_once_with(order)
        broker.placeOrder.assert_not_called()
    else:
        assert adapter.place_order(contract, order) is broker.placeOrder.return_value
        broker.placeOrder.assert_called_once_with(contract, order)
        broker.cancelOrder.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_keeps_account_reads_available(broker: Mock) -> None:
    adapter = IBKR(broker, 1, "SMART", dry_run=True)
    broker.accountSummaryAsync.return_value = []
    broker.portfolio.return_value = []

    assert await adapter.account_summary("DU-TEST") == []
    assert adapter.portfolio("DU-TEST") == []
    adapter.set_market_data_type(1)

    broker.accountSummaryAsync.assert_awaited_once_with("DU-TEST")
    broker.reqMarketDataType.assert_called_once_with(1)
    broker.placeOrder.assert_not_called()
    broker.cancelOrder.assert_not_called()


@pytest.mark.parametrize(
    "account,order_ref,symbol,status,should_cancel",
    [
        ("DU-TEST", "tg:order", "SPY", "Submitted", True),
        ("DU-TEST", "tg:regime-rebalance:SPY", "SPY", "Submitted", True),
        ("DU-TEST", "tg:order", "VIX", "Submitted", True),
        ("DU-TEST", "tg:order", "SGOV", "Submitted", True),
        ("DU-OTHER", "tg:order", "SPY", "Submitted", False),
        ("", "tg:order", "SPY", "Submitted", False),
        ("DU-TEST", "", "SPY", "Submitted", False),
        ("DU-TEST", "manual", "SPY", "Submitted", False),
        ("DU-TEST", "tg-other", "SPY", "Submitted", False),
        ("DU-TEST", "tg:order", "QQQ", "Submitted", False),
        ("DU-TEST", "tg:order", "SPY", "Filled", False),
        ("DU-TEST", "tg:order", "SPY", "Cancelled", False),
    ],
)
def test_startup_cancellation_requires_account_ownership_and_managed_symbol(
    manager: PortfolioManager,
    broker: Mock,
    account: str,
    order_ref: str,
    symbol: str,
    status: str,
    should_cancel: bool,
) -> None:
    order = LimitOrder("BUY", 1, 100, account=account, orderRef=order_ref)
    broker.openTrades.return_value = [
        Trade(Stock(symbol, "SMART", "USD"), order, OrderStatus(status=status))
    ]

    manager.initialize_account()

    if should_cancel:
        broker.cancelOrder.assert_called_once_with(order)
    else:
        broker.cancelOrder.assert_not_called()
    broker.placeOrder.assert_not_called()


@pytest.mark.parametrize("symbol", ["VIX", "SGOV"])
def test_disabled_post_strategy_orders_are_not_canceled(
    manager: PortfolioManager, broker: Mock, symbol: str
) -> None:
    manager.config.strategies.vix_call_hedge.enabled = False
    manager.config.strategies.cash_management.enabled = False
    broker.openTrades.return_value = [
        Trade(
            Stock(symbol, "SMART", "USD"),
            LimitOrder("BUY", 1, 100, account="DU-TEST", orderRef="tg:order"),
            OrderStatus(status="Submitted"),
        )
    ]
    manager.initialize_account()
    broker.cancelOrder.assert_not_called()


def test_cancel_orders_false_keeps_existing_orders(
    manager: PortfolioManager, broker: Mock
) -> None:
    manager.config.runtime.account.cancel_orders = False
    manager.initialize_account()
    broker.openTrades.assert_not_called()
    broker.cancelOrder.assert_not_called()


@pytest.mark.parametrize("order_ref", [None, "", "tg:regime-rebalance:SPY"])
def test_order_creation_sets_and_preserves_ownership(
    manager: PortfolioManager, order_ref: str | None
) -> None:
    order = manager.order_ops.create_limit_order(
        action="BUY", quantity=1, limit_price=100, order_ref=order_ref
    )
    assert order.account == "DU-TEST"
    assert order.orderRef == (order_ref or DEFAULT_ORDER_REF)


@pytest.mark.asyncio
async def test_dry_run_previews_orders_without_broker_writes(
    config: Config, broker: Mock, mocker: MockerFixture
) -> None:
    data_store = mocker.Mock()
    data_store.record_order_intent.return_value = 51
    manager = PortfolioManager(
        config,
        broker,
        asyncio.get_running_loop().create_future(),
        dry_run=True,
        data_store=data_store,
        run_stage_order=[],
    )
    mocker.patch.object(manager, "summarize_account", return_value=({}, {}))
    mocker.patch.object(manager, "options_trading_enabled", return_value=False)
    preview = mocker.spy(manager.orders, "print_summary")
    contract = Stock("SPY", "SMART", "USD")
    order = manager.order_ops.create_limit_order(
        action="BUY", quantity=1, limit_price=100
    )
    manager.order_ops.enqueue_order(contract, order)
    broker.openTrades.return_value = [
        Trade(contract, order, OrderStatus(status="Submitted"))
    ]

    await manager.manage()

    preview.assert_called_once()
    assert manager.orders.records() == [(contract, order, 51)]
    data_store.record_order_intent.assert_called_once_with(contract, order)
    data_store.record_order.assert_not_called()
    broker.reqMarketDataType.assert_called_once()
    broker.placeOrder.assert_not_called()
    broker.cancelOrder.assert_not_called()

    # Even a caller bypassing manage's preview branch cannot send the queue.
    with pytest.raises(DryRunOrderError):
        manager.submit_orders()
    assert manager.trades.is_empty()
    data_store.record_order.assert_not_called()
    broker.placeOrder.assert_not_called()
    broker.cancelOrder.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_repricing_keeps_ownership_and_respects_dry_run(
    config: Config, broker: Mock, mocker: MockerFixture, dry_run: bool
) -> None:
    manager = PortfolioManager(config, broker, Mock(), dry_run=dry_run)
    contract = Stock("SPY", "SMART", "USD")
    original = manager.order_ops.create_limit_order(
        action="BUY",
        quantity=1,
        limit_price=100,
        order_id=42,
        order_ref="tg:regime-rebalance:SPY",
        tif="GTC",
    )
    original.clientId = 7
    original.outsideRth = True
    original.ocaGroup = "test-group"
    trade = Trade(contract, original, OrderStatus(status="Submitted"))
    if not dry_run:
        broker.placeOrder.return_value = trade
        manager.trades.submit_order(contract, original)
        broker.placeOrder.reset_mock()
    mocker.patch.object(manager.trades, "records", return_value=[trade])
    mocker.patch.object(manager.trades, "is_empty", return_value=False)
    mocker.patch.object(manager.ibkr, "wait_for_orders_complete")
    mocker.patch.object(
        manager.ibkr,
        "get_ticker_for_contract",
        return_value=SimpleNamespace(midpoint=lambda: 102.0),
    )

    await manager.adjust_prices()

    assert original.lmtPrice == 100
    if dry_run:
        broker.placeOrder.assert_not_called()
    else:
        broker.placeOrder.assert_called_once()
        updated = broker.placeOrder.call_args.args[1]
        assert updated is not original
        assert asdict(updated) == {**asdict(original), "lmtPrice": 101.0}
        # Repriced orders still satisfy startup's ownership filter.
        broker.openTrades.return_value = [
            Trade(contract, updated, OrderStatus(status="Submitted"))
        ]
        manager.initialize_account()
        broker.cancelOrder.assert_called_once_with(updated)
