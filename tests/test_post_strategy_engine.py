from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from ib_async import IB, AccountValue, MarketOrder
from ib_async.contract import Contract, Option, Stock

from thetagang.config import Config
from thetagang.ibkr import IBKR
from thetagang.orders import order_cash_notional
from thetagang.strategies.post_engine import PostStrategyEngine
from thetagang.strategies.tail_hedge_state import (
    TAIL_HEDGE_CLOSE_ORDER_REF,
    TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX,
)


def _make_engine(mocker):
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            account=SimpleNamespace(number="TEST123"),
            orders=SimpleNamespace(
                minimum_credit=0.0,
                algo=SimpleNamespace(strategy="Adaptive", params=[]),
            ),
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
    ibkr.open_trades.return_value = []
    ibkr.account_summary = AsyncMock(return_value=[])
    ibkr.cached_account_value.return_value = 1000.0
    ibkr.portfolio.return_value = []
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


def test_pending_cash_includes_all_queued_order_cash_flows(mocker):
    engine, _ibkr, _order_ops, _scanner = _make_engine(mocker)
    stock = Stock("AAA", "SMART", "USD")
    tail_put = Option("AAA", "20270115", 100.0, "P", "SMART")
    tail_put.multiplier = "100"
    engine.orders.records.return_value = [
        (
            stock,
            SimpleNamespace(
                action="BUY",
                lmtPrice=100.0,
                totalQuantity=1,
                orderRef="tg:regime-rebalance:AAA",
            ),
            None,
        ),
        (
            tail_put,
            SimpleNamespace(
                action="SELL",
                lmtPrice=2.0,
                totalQuantity=1,
                orderRef=f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:AAA:801",
            ),
            None,
        ),
        (
            tail_put,
            SimpleNamespace(
                action="SELL",
                lmtPrice=3.0,
                totalQuantity=1,
                orderRef=TAIL_HEDGE_CLOSE_ORDER_REF,
            ),
            None,
        ),
        (
            stock,
            SimpleNamespace(
                action="SELL",
                lmtPrice=50.0,
                totalQuantity=1,
                orderRef="ordinary-sale",
            ),
            None,
        ),
    ]

    assert engine.calc_pending_cash_balance() == 450.0


def test_pending_cash_uses_signed_combo_order_value(mocker):
    engine, _ibkr, _order_ops, _scanner = _make_engine(mocker)
    combo = Contract(secType="BAG", symbol="AAA", multiplier="100")
    engine.orders.records.return_value = [
        (
            combo,
            SimpleNamespace(action="BUY", lmtPrice=-1.0, totalQuantity=1),
            None,
        ),
        (
            combo,
            SimpleNamespace(action="SELL", lmtPrice=-0.5, totalQuantity=1),
            None,
        ),
    ]

    assert engine.pending_cash_components() == (50.0, 100.0)
    assert engine.calc_pending_cash_balance() == 50.0


def test_pending_option_cash_includes_estimated_per_contract_fees(mocker):
    engine, _ibkr, _order_ops, _scanner = _make_engine(mocker)
    engine.config.runtime.orders.estimated_fee_per_contract = 1.0
    option = Option("AAA", "20270115", 100.0, "P", "SMART")
    option.multiplier = "100"
    engine.orders.records.return_value = [
        (
            option,
            SimpleNamespace(action="BUY", lmtPrice=1.0, totalQuantity=2),
            None,
        ),
        (
            option,
            SimpleNamespace(action="SELL", lmtPrice=2.0, totalQuantity=1),
            None,
        ),
    ]

    assert engine.pending_cash_components() == (202.0, 199.0)


@pytest.mark.asyncio
async def test_do_cashman_accounts_for_same_run_regime_sale_proceeds(mocker):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    engine.config.runtime.orders.estimated_fee_per_contract = 1.0
    engine.config.strategies.cash_management.target_cash_balance = 5000.0
    engine.config.strategies.cash_management.buy_threshold = 5000.0
    engine.config.strategies.cash_management.sell_threshold = 5000.0
    engine.config.strategies.cash_management.cash_fund = "SHV"
    regime_sales = [
        ("TQQQ", 72.10, 53),
        ("IBIT", 36.61, 135),
        ("BTAL", 12.11, 359),
        ("CTA", 27.98, 123),
    ]
    engine.orders.records.return_value = [
        (
            Stock(symbol, "SMART", "USD"),
            SimpleNamespace(
                action="SELL",
                lmtPrice=price,
                totalQuantity=quantity,
                orderRef=f"tg:regime-rebalance:{symbol}",
            ),
            None,
        )
        for symbol, price, quantity in regime_sales
    ]

    await engine.do_cashman(
        {"TotalCashValue": SimpleNamespace(value="-11649")},
        {
            "SHV": [
                SimpleNamespace(
                    contract=Stock("SHV", "SMART", "USD"),
                    position=138,
                )
            ]
        },
    )

    assert engine.calc_pending_cash_balance() == pytest.approx(16552.68)
    ibkr.get_ticker_for_stock.assert_not_called()
    order_ops.create_limit_order.assert_not_called()
    order_ops.enqueue_order.assert_not_called()


@pytest.mark.asyncio
async def test_do_cashman_does_not_buy_cash_fund_from_queued_stock_sales(mocker):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    stock = Stock("AAA", "SMART", "USD")
    engine.orders.records.return_value = [
        (
            stock,
            SimpleNamespace(
                action="SELL",
                lmtPrice=100.0,
                totalQuantity=20,
                orderRef="tg:regime-rebalance:AAA",
            ),
            None,
        )
    ]

    await engine.do_cashman(
        {"TotalCashValue": SimpleNamespace(value="0")},
        {},
    )

    assert engine.calc_pending_cash_balance() == 2000.0
    ibkr.get_ticker_for_stock.assert_not_called()
    order_ops.create_limit_order.assert_not_called()
    order_ops.enqueue_order.assert_not_called()


@pytest.mark.asyncio
async def test_do_cashman_sells_only_remaining_deficit_after_queued_stock_sales(
    mocker,
):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    engine.config.strategies.cash_management.target_cash_balance = 5000.0
    engine.config.strategies.cash_management.buy_threshold = 5000.0
    engine.config.strategies.cash_management.sell_threshold = 5000.0
    engine.config.strategies.cash_management.cash_fund = "SHV"
    engine.orders.records.return_value = [
        (
            Stock("AAA", "SMART", "USD"),
            SimpleNamespace(
                action="SELL",
                lmtPrice=100.0,
                totalQuantity=100,
                orderRef="tg:regime-rebalance:AAA",
            ),
            None,
        )
    ]
    ticker = SimpleNamespace(
        contract=Stock("SHV", "SMART", "USD"),
        ask=100.0,
        bid=100.0,
    )
    ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    ibkr.cached_account_value.return_value = -20000.0
    ibkr.portfolio.return_value = [
        SimpleNamespace(
            contract=Stock("SHV", "SMART", "USD"),
            position=200,
        )
    ]

    await engine.do_cashman(
        {"TotalCashValue": SimpleNamespace(value="-20000")},
        {
            "SHV": [
                SimpleNamespace(
                    contract=Stock("SHV", "SMART", "USD"),
                    position=200,
                )
            ]
        },
    )

    order_ops.create_limit_order.assert_called_once()
    assert order_ops.create_limit_order.call_args.kwargs["action"] == "SELL"
    assert order_ops.create_limit_order.call_args.kwargs["quantity"] == 150
    order_ops.enqueue_order.assert_called_once_with(ticker.contract, "ORDER")


def test_pending_cash_uses_valid_remaining_quantity_for_buys_and_sells(
    mocker,
):
    engine, ibkr, _order_ops, _scanner = _make_engine(mocker)
    tail_put = Option("AAA", "20270115", 100.0, "P", "SMART")
    tail_put.multiplier = "100"

    def trade(*, account="TEST123", action="BUY", done=False):
        return SimpleNamespace(
            contract=tail_put,
            order=SimpleNamespace(
                account=account,
                action=action,
                lmtPrice=2.0,
                totalQuantity=3,
            ),
            orderStatus=SimpleNamespace(remaining=1.0),
            isDone=lambda: done,
        )

    ibkr.open_trades.return_value = [
        trade(),
        trade(account="OTHER"),
        trade(action="SELL"),
        trade(done=True),
    ]

    assert engine.calc_pending_cash_balance() == 0.0


def test_pending_cash_uses_working_stock_sell_remaining_quantity(mocker):
    engine, ibkr, _order_ops, _scanner = _make_engine(mocker)
    stock = Stock("AAA", "SMART", "USD")
    ibkr.open_trades.return_value = [
        SimpleNamespace(
            contract=stock,
            order=SimpleNamespace(
                account="TEST123",
                action="SELL",
                orderType="LMT",
                lmtPrice=100.0,
                totalQuantity=3,
            ),
            orderStatus=SimpleNamespace(remaining=1),
            isDone=lambda: False,
        )
    ]

    assert engine.pending_cash_components() == (0.0, 100.0)


@pytest.mark.parametrize("remaining", [float("nan"), 0.0])
def test_pending_cash_rejects_ambiguous_remaining_quantity(mocker, remaining):
    engine, ibkr, _order_ops, _scanner = _make_engine(mocker)
    tail_put = Option("AAA", "20270115", 100.0, "P", "SMART")
    tail_put.multiplier = "100"
    ibkr.open_trades.return_value = [
        SimpleNamespace(
            contract=tail_put,
            order=SimpleNamespace(
                account="TEST123",
                action="BUY",
                lmtPrice=2.0,
                totalQuantity=3,
            ),
            orderStatus=SimpleNamespace(remaining=remaining),
            isDone=lambda: False,
        )
    ]

    with pytest.raises(RuntimeError, match="cannot be priced safely"):
        engine.calc_pending_cash_balance()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["BUY", "SELL"])
async def test_market_order_blocks_cash_management(mocker, action):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    ibkr.open_trades.return_value = [
        SimpleNamespace(
            contract=Stock("AAA", "SMART", "USD"),
            order=MarketOrder(action, 1, account="TEST123"),
            orderStatus=SimpleNamespace(remaining=1),
            isDone=lambda: False,
        )
    ]

    await engine.do_cashman(
        {"TotalCashValue": SimpleNamespace(value="0")},
        {},
    )

    ibkr.get_ticker_for_stock.assert_not_called()
    order_ops.enqueue_order.assert_not_called()


def test_order_cash_notional_rejects_overflow():
    option = Option("AAA", "20270115", 100.0, "P", "SMART")
    option.multiplier = "100"
    order = SimpleNamespace(
        action="BUY",
        orderType="LMT",
        lmtPrice=1e308,
        totalQuantity=2,
    )

    with pytest.raises(ValueError, match="finite"):
        order_cash_notional(option, order)


@pytest.mark.parametrize(
    ("price", "quantity"),
    [(0.0, 1), (1.0, 0)],
)
def test_pending_cash_rejects_unpriceable_queued_limit_order(
    mocker,
    price,
    quantity,
):
    engine, _ibkr, _order_ops, _scanner = _make_engine(mocker)
    engine.orders.records.return_value = [
        (
            Stock("AAA", "SMART", "USD"),
            SimpleNamespace(
                action="BUY",
                orderType="LMT",
                lmtPrice=price,
                totalQuantity=quantity,
            ),
            None,
        )
    ]

    with pytest.raises(RuntimeError, match="cannot be priced safely"):
        engine.pending_cash_components()


@pytest.mark.asyncio
async def test_do_tail_hedging_sizes_from_account_net_liquidation(mocker):
    engine, _ibkr, _order_ops, _scanner = _make_engine(mocker)
    engine.tail_hedge_engine.manage = AsyncMock()
    positions = {"QQQ": []}

    await engine.do_tail_hedging(
        {"NetLiquidation": SimpleNamespace(value="123456.78")},
        positions,
    )

    engine.tail_hedge_engine.manage.assert_awaited_once_with(
        positions,
        net_liquidation=123456.78,
    )


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
    ibkr.cached_account_value.return_value = 2000.0

    await engine.do_cashman(account_summary, {})

    order_ops.create_limit_order.assert_called_once()
    assert order_ops.create_limit_order.call_args.kwargs["action"] == "BUY"
    assert order_ops.create_limit_order.call_args.kwargs["quantity"] > 0
    order_ops.enqueue_order.assert_called_once_with(ticker.contract, "ORDER")


@pytest.mark.asyncio
async def test_do_cashman_reserved_regime_cash_suppresses_cash_fund_buy(mocker):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    engine._get_reserved_cash_for_post_management = lambda: 4100.0
    account_summary = {"TotalCashValue": SimpleNamespace(value="8400")}
    stock_contract = Stock("AAA", "SMART", "USD")
    stock_order = SimpleNamespace(action="BUY", lmtPrice=100.0, totalQuantity=43)
    engine.orders.records = mocker.Mock(
        return_value=[(stock_contract, stock_order, None)]
    )

    await engine.do_cashman(account_summary, {})

    ibkr.get_ticker_for_stock.assert_not_called()
    order_ops.create_limit_order.assert_not_called()
    order_ops.enqueue_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cash_balance", "expected_quantity"),
    [("0", 10), ("50", 10)],
)
async def test_do_cashman_cash_deficit_uses_ceiling_share_quantity(
    mocker,
    cash_balance,
    expected_quantity,
):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    account_summary = {"TotalCashValue": SimpleNamespace(value=cash_balance)}
    ticker = SimpleNamespace(contract=Contract(), ask=100.0, bid=100.0)
    ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    ibkr.cached_account_value.return_value = float(cash_balance)
    portfolio_positions = {
        "SGOV": [SimpleNamespace(contract=Stock("SGOV", "SMART", "USD"), position=20)]
    }
    ibkr.portfolio.return_value = portfolio_positions["SGOV"]

    await engine.do_cashman(account_summary, portfolio_positions)

    order_ops.create_limit_order.assert_called_once()
    assert order_ops.create_limit_order.call_args.kwargs["action"] == "SELL"
    assert (
        int(order_ops.create_limit_order.call_args.kwargs["quantity"])
        == expected_quantity
    )


@pytest.mark.asyncio
async def test_do_cashman_reserved_regime_cash_does_not_block_cash_fund_sell(mocker):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    engine._get_reserved_cash_for_post_management = lambda: 5000.0
    account_summary = {"TotalCashValue": SimpleNamespace(value="0")}
    ticker = SimpleNamespace(contract=Contract(), ask=100.0, bid=100.0)
    ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    ibkr.cached_account_value.return_value = 0.0
    portfolio_positions = {
        "SGOV": [SimpleNamespace(contract=Stock("SGOV", "SMART", "USD"), position=10)]
    }
    ibkr.portfolio.return_value = portfolio_positions["SGOV"]

    await engine.do_cashman(account_summary, portfolio_positions)

    order_ops.create_limit_order.assert_called_once()
    assert order_ops.create_limit_order.call_args.kwargs["action"] == "SELL"
    assert order_ops.create_limit_order.call_args.kwargs["quantity"] > 0


@pytest.mark.asyncio
async def test_do_cashman_skips_zero_share_cash_fund_buy(mocker):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    ticker = SimpleNamespace(contract=Contract(), ask=200.0, bid=199.0)
    ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    ibkr.cached_account_value.return_value = 1150.0

    await engine.do_cashman(
        {"TotalCashValue": SimpleNamespace(value="1150")},
        {},
    )

    order_ops.create_limit_order.assert_not_called()
    order_ops.enqueue_order.assert_not_called()


@pytest.mark.asyncio
async def test_do_cashman_rereads_cash_after_ticker_await(mocker):
    engine, _ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    ticker = SimpleNamespace(contract=Contract(), ask=100.0, bid=100.0)
    live_ib = IB()
    ibkr = IBKR(live_ib, 1, "SMART")
    engine.ibkr = ibkr

    async def quote(*_args):
        live_ib.wrapper.accountValues[("TEST123", "TotalCashValue", "BASE", "")] = (
            AccountValue("TEST123", "TotalCashValue", "1000", "BASE", "")
        )
        return ticker

    mocker.patch.object(ibkr, "get_ticker_for_stock", side_effect=quote)
    account_values = mocker.spy(live_ib, "accountValues")
    account_summary_async = mocker.spy(live_ib, "accountSummaryAsync")

    await engine.do_cashman(
        {"TotalCashValue": SimpleNamespace(value="0")},
        {
            "SGOV": [
                SimpleNamespace(
                    contract=Stock("SGOV", "SMART", "USD"),
                    position=20,
                )
            ]
        },
    )

    account_values.assert_called_once_with("TEST123")
    account_summary_async.assert_not_called()
    order_ops.create_limit_order.assert_not_called()


@pytest.mark.asyncio
async def test_do_cashman_rechecks_working_fund_order_after_ticker_await(mocker):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    contract = Stock("SGOV", "SMART", "USD")
    trade = SimpleNamespace(
        contract=contract,
        order=SimpleNamespace(account="TEST123", action="SELL"),
        isDone=lambda: False,
    )

    async def quote(*_args):
        ibkr.open_trades.return_value = [trade]
        return SimpleNamespace(contract=contract, ask=100.0, bid=100.0)

    ibkr.get_ticker_for_stock = AsyncMock(side_effect=quote)

    await engine.do_cashman(
        {"TotalCashValue": SimpleNamespace(value="0")},
        {"SGOV": [SimpleNamespace(contract=contract, position=20)]},
    )

    order_ops.create_limit_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["broker", "local"])
async def test_do_cashman_skips_existing_cash_fund_order(mocker, source):
    engine, ibkr, order_ops, _scanner = _make_engine(mocker)
    engine.config.strategies.cash_management.enabled = True
    contract = Stock("SGOV", "SMART", "USD")
    order = SimpleNamespace(account="TEST123", action="SELL")
    if source == "broker":
        ibkr.open_trades.return_value = [
            SimpleNamespace(
                contract=contract,
                order=order,
                isDone=lambda: False,
            )
        ]
    else:
        engine.orders.records.return_value = [(contract, order, None)]

    await engine.do_cashman(
        {"TotalCashValue": SimpleNamespace(value="0")},
        {"SGOV": [SimpleNamespace(contract=contract, position=20)]},
    )

    ibkr.get_ticker_for_stock.assert_not_called()
    order_ops.create_limit_order.assert_not_called()


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
