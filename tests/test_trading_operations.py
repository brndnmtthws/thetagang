from types import SimpleNamespace
from typing import cast

import pytest
from ib_async.order import LimitOrder

from thetagang.config import Config
from thetagang.orders import Orders
from thetagang.trading_operations import OrderOperations


def test_order_operations_round_vix_price() -> None:
    config = cast(
        Config,
        SimpleNamespace(
            runtime=SimpleNamespace(
                orders=SimpleNamespace(
                    algo=SimpleNamespace(strategy="Adaptive", params=[]),
                    exchange="SMART",
                )
            )
        ),
    )
    orders = cast(Orders, SimpleNamespace(add_order=lambda *args, **kwargs: None))
    ops = OrderOperations(
        config=config,
        account_number="DUX",
        orders=orders,
        data_store=None,
    )
    assert ops.round_vix_price(2.034) == pytest.approx(2.03)
    assert ops.round_vix_price(3.021) == pytest.approx(3.0)
    assert ops.round_vix_price(3.03) == pytest.approx(3.05)


def test_order_operations_enqueue_order_records_order(mocker) -> None:
    orders = mocker.Mock()
    data_store = mocker.Mock()
    config = mocker.Mock()
    config.runtime.orders.algo.strategy = "Adaptive"
    config.runtime.orders.algo.params = []
    config.runtime.orders.exchange = "SMART"
    ops = OrderOperations(
        config=config,
        account_number="DUX",
        orders=orders,
        data_store=data_store,
    )
    contract = mocker.Mock(
        symbol="AAPL", secType="STK", conId=1, exchange="SMART", currency="USD"
    )
    order = LimitOrder("BUY", 1, 100.0)

    ops.enqueue_order(contract, order)

    orders.add_order.assert_called_once()
    data_store.record_order_intent.assert_called_once()
    data_store.record_event.assert_called_once()
