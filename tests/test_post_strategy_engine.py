from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from ib_async.contract import Contract, Option, Stock

from thetagang.config import Config
from thetagang.strategies.post_engine import PostStrategyEngine


def _make_engine(mocker):
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            orders=SimpleNamespace(
                minimum_credit=0.0,
                algo=SimpleNamespace(strategy="Adaptive", params=[]),
            )
        ),
        strategies=SimpleNamespace(
            vix_call_hedge=SimpleNamespace(
                enabled=False,
                close_hedges_when_vix_exceeds=None,
                ignore_dte=0,
                allocation=[],
                delta=0.3,
                target_dte=30,
            ),
            cash_management=SimpleNamespace(
                enabled=False,
                target_cash_balance=1000.0,
                buy_threshold=100.0,
                sell_threshold=100.0,
                cash_fund="SGOV",
                primary_exchange="ARCA",
                orders=SimpleNamespace(
                    exchange="SMART",
                    algo=SimpleNamespace(strategy="Adaptive", params=[]),
                ),
            ),
        ),
    )
    ibkr = mocker.Mock()
    order_ops = mocker.Mock()
    order_ops.create_limit_order = mocker.Mock(return_value="ORDER")
    order_ops.enqueue_order = mocker.Mock()
    order_ops.algo_params_from = mocker.Mock(return_value=[])
    order_ops.round_vix_price = mocker.Mock(side_effect=lambda x: x)
    option_scanner = mocker.Mock()
    option_scanner.find_eligible_contracts = AsyncMock()
    orders = mocker.Mock()
    orders.records = mocker.Mock(return_value=[])
    return (
        PostStrategyEngine(
            config=cast(Config, config),
            ibkr=ibkr,
            order_ops=order_ops,
            option_scanner=option_scanner,
            orders=orders,
            qualified_contracts={},
        ),
        ibkr,
        order_ops,
        option_scanner,
    )


@pytest.mark.asyncio
async def test_do_vix_hedging_disabled_noops(mocker):
    engine, _ibkr, order_ops, _scanner = _make_engine(mocker)

    await engine.do_vix_hedging({}, {})

    order_ops.create_limit_order.assert_not_called()
    order_ops.enqueue_order.assert_not_called()


@pytest.mark.asyncio
async def test_do_cashman_disabled_noops(mocker):
    engine, _ibkr, order_ops, _scanner = _make_engine(mocker)

    await engine.do_cashman({}, {})

    order_ops.create_limit_order.assert_not_called()
    order_ops.enqueue_order.assert_not_called()


@pytest.mark.asyncio
async def test_do_cashman_within_threshold_no_order(mocker):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    account_summary = {"TotalCashValue": SimpleNamespace(value="1050")}

    await engine.do_cashman(account_summary, {})

    ibkr.get_ticker_for_stock.assert_not_called()
    order_ops.create_limit_order.assert_not_called()


@pytest.mark.asyncio
async def test_do_cashman_excess_cash_buys_cash_fund(mocker):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    account_summary = {"TotalCashValue": SimpleNamespace(value="2000")}
    ticker = SimpleNamespace(contract=Contract(), ask=100.0, bid=99.0)
    ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)

    await engine.do_cashman(account_summary, {})

    order_ops.create_limit_order.assert_called_once()
    assert order_ops.create_limit_order.call_args.kwargs["action"] == "BUY"
    assert order_ops.create_limit_order.call_args.kwargs["quantity"] > 0
    order_ops.enqueue_order.assert_called_once_with(ticker.contract, "ORDER")


@pytest.mark.asyncio
async def test_do_cashman_cash_deficit_sells_cash_fund(mocker):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    account_summary = {"TotalCashValue": SimpleNamespace(value="0")}
    ticker = SimpleNamespace(contract=Contract(), ask=100.0, bid=100.0)
    ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    portfolio_positions = {
        "SGOV": [SimpleNamespace(contract=Stock("SGOV", "SMART", "USD"), position=10)]
    }

    await engine.do_cashman(account_summary, portfolio_positions)

    order_ops.create_limit_order.assert_called_once()
    assert order_ops.create_limit_order.call_args.kwargs["action"] == "SELL"
    assert order_ops.create_limit_order.call_args.kwargs["quantity"] > 0


@pytest.mark.asyncio
async def test_do_vix_hedging_closes_existing_calls_when_threshold_hit(mocker):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.vix_call_hedge.enabled = True
    engine.config.strategies.vix_call_hedge.close_hedges_when_vix_exceeds = 20.0
    mocker.patch(
        "thetagang.strategies.post_engine.net_option_positions", return_value=1
    )
    mocker.patch("thetagang.strategies.post_engine.get_lower_price", return_value=2.0)
    ibkr.get_ticker_for_contract = AsyncMock(
        side_effect=[
            SimpleNamespace(marketPrice=lambda: 30.0),
            SimpleNamespace(contract=Contract()),
        ]
    )
    vix_call = SimpleNamespace(
        contract=Option(
            symbol="VIX",
            lastTradeDateOrContractMonth="20270115",
            strike=20.0,
            right="C",
            exchange="SMART",
            currency="USD",
        ),
        position=1,
    )

    await engine.do_vix_hedging(
        {"NetLiquidation": SimpleNamespace(value="100000")},
        {"VIX": [vix_call]},
    )

    order_ops.create_limit_order.assert_called_once()
    assert order_ops.create_limit_order.call_args.kwargs["action"] == "SELL"
    assert order_ops.create_limit_order.call_args.kwargs["quantity"] == 1


@pytest.mark.asyncio
async def test_do_vix_hedging_buys_new_hedge_from_allocation_band(mocker):
    engine, ibkr, order_ops, scanner = _make_engine(mocker)
    engine.config.strategies.vix_call_hedge.enabled = True
    engine.config.strategies.vix_call_hedge.close_hedges_when_vix_exceeds = None
    engine.config.strategies.vix_call_hedge.allocation = [
        SimpleNamespace(lower_bound=0.0, upper_bound=100.0, weight=0.01)
    ]
    mocker.patch(
        "thetagang.strategies.post_engine.net_option_positions", return_value=0
    )
    mocker.patch("thetagang.strategies.post_engine.get_lower_price", return_value=5.0)
    ibkr.get_ticker_for_contract = AsyncMock(
        return_value=SimpleNamespace(marketPrice=lambda: 15.0)
    )
    contract = Option(
        symbol="VIX",
        lastTradeDateOrContractMonth="20270115",
        strike=20.0,
        right="C",
        exchange="SMART",
        currency="USD",
    )
    contract.multiplier = "100"
    scanner.find_eligible_contracts = AsyncMock(
        return_value=SimpleNamespace(contract=contract)
    )

    await engine.do_vix_hedging({"NetLiquidation": SimpleNamespace(value="100000")}, {})

    order_ops.create_limit_order.assert_called_once()
    assert order_ops.create_limit_order.call_args.kwargs["action"] == "BUY"
    assert order_ops.create_limit_order.call_args.kwargs["quantity"] == 2
