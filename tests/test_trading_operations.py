from types import SimpleNamespace
from typing import cast

import pytest

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


@pytest.mark.parametrize("order_ref", [None, "tg:regime-rebalance:AAPL"])
def test_order_operations_records_owned_order(mocker, order_ref) -> None:
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
    order = ops.create_limit_order(
        action="BUY", quantity=1, limit_price=100.0, order_ref=order_ref
    )

    ops.enqueue_order(contract, order)

    persisted_order = data_store.record_order_intent.call_args.args[1]
    assert persisted_order.orderRef == (order_ref or "tg:order")
    assert orders.add_order.call_args.args[1].orderRef == persisted_order.orderRef
