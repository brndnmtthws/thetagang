import math
from datetime import date, timedelta

from ib_insync import Option, Order, PortfolioItem
from ib_insync.contract import Stock

from thetagang.util import (
    calculate_net_short_positions,
    get_target_delta,
    position_pnl,
    weighted_avg_long_strike,
    weighted_avg_short_strike,
    would_increase_spread,
)


def test_position_pnl() -> None:
    qqq_put = PortfolioItem(
        contract=Option(
            conId=397556522,
            symbol="QQQ",
            lastTradeDateOrContractMonth="20201218",
            strike=300.0,
            right="P",
            multiplier="100",
            primaryExchange="AMEX",
            currency="USD",
            localSymbol="QQQ   201218P00300000",
            tradingClass="QQQ",
        ),
        position=-1.0,
        marketPrice=4.1194396,
        marketValue=-411.94,
        averageCost=222.4293,
        unrealizedPNL=-189.51,
        realizedPNL=0.0,
        account="DU2962946",
    )
    assert round(position_pnl(qqq_put), 2) == -0.85

    spy = PortfolioItem(
        contract=Stock(
            conId=756733,
            symbol="SPY",
            right="0",
            primaryExchange="ARCA",
            currency="USD",
            localSymbol="SPY",
            tradingClass="SPY",
        ),
        position=100.0,
        marketPrice=365.4960022,
        marketValue=36549.6,
        averageCost=368.42,
        unrealizedPNL=-292.4,
        realizedPNL=0.0,
        account="DU2962946",
    )
    assert round(position_pnl(spy), 4) == -0.0079

    spy_call = PortfolioItem(
        contract=Option(
            conId=454208258,
            symbol="SPY",
            lastTradeDateOrContractMonth="20201214",
            strike=373.0,
            right="C",
            multiplier="100",
            primaryExchange="AMEX",
            currency="USD",
            localSymbol="SPY   201214C00373000",
            tradingClass="SPY",
        ),
        position=-1.0,
        marketPrice=0.08,
        marketValue=-8.0,
        averageCost=96.422,
        unrealizedPNL=88.42,
        realizedPNL=0.0,
        account="DU2962946",
    )
    assert round(position_pnl(spy_call), 2) == 0.92

    spy_put = PortfolioItem(
        contract=Option(
            conId=458705534,
            symbol="SPY",
            lastTradeDateOrContractMonth="20210122",
            strike=352.5,
            right="P",
            multiplier="100",
            primaryExchange="AMEX",
            currency="USD",
            localSymbol="SPY   210122P00352500",
            tradingClass="SPY",
        ),
        position=-1.0,
        marketPrice=5.96710015,
        marketValue=-596.71,
        averageCost=528.9025,
        unrealizedPNL=-67.81,
        realizedPNL=0.0,
        account="DU2962946",
    )
    assert round(position_pnl(spy_put), 2) == -0.13


def test_get_delta() -> None:
    config = {"target": {"delta": 0.5}, "symbols": {"SPY": {"weight": 1}}}
    assert 0.5 == get_target_delta(config, "SPY", "P")

    config = {
        "target": {"delta": 0.5, "puts": {"delta": 0.4}},
        "symbols": {"SPY": {"weight": 1}},
    }
    assert 0.4 == get_target_delta(config, "SPY", "P")

    config = {
        "target": {"delta": 0.5, "calls": {"delta": 0.4}},
        "symbols": {"SPY": {"weight": 1}},
    }
    assert 0.5 == get_target_delta(config, "SPY", "P")

    config = {
        "target": {"delta": 0.5, "calls": {"delta": 0.4}},
        "symbols": {"SPY": {"weight": 1}},
    }
    assert 0.4 == get_target_delta(config, "SPY", "C")

    config = {
        "target": {"delta": 0.5, "calls": {"delta": 0.4}},
        "symbols": {"SPY": {"weight": 1, "delta": 0.3}},
    }
    assert 0.3 == get_target_delta(config, "SPY", "C")

    config = {
        "target": {"delta": 0.5, "calls": {"delta": 0.4}},
        "symbols": {"SPY": {"weight": 1, "delta": 0.3, "puts": {"delta": 0.2}}},
    }
    assert 0.3 == get_target_delta(config, "SPY", "C")

    config = {
        "target": {"delta": 0.5, "calls": {"delta": 0.4}},
        "symbols": {"SPY": {"weight": 1, "delta": 0.3, "puts": {"delta": 0.2}}},
    }
    assert 0.2 == get_target_delta(config, "SPY", "P")


def con(dte: str, strike: float, right: str, position: float) -> PortfolioItem:
    return PortfolioItem(
        contract=Option(
            conId=458705534,
            symbol="SPY",
            lastTradeDateOrContractMonth=dte,
            strike=strike,
            right=right,
            multiplier="100",
            primaryExchange="AMEX",
            currency="USD",
            localSymbol="SPY   210122P00352500",
            tradingClass="SPY",
        ),
        position=position,
        marketPrice=5.96710015,
        marketValue=-596.71,
        averageCost=528.9025,
        unrealizedPNL=-67.81,
        realizedPNL=0.0,
        account="DU2962946",
    )


def test_calculate_net_short_positions() -> None:
    today = date.today()
    exp3dte = (today + timedelta(days=3)).strftime("%Y%m%d")
    exp30dte = (today + timedelta(days=30)).strftime("%Y%m%d")
    exp90dte = (today + timedelta(days=90)).strftime("%Y%m%d")

    assert 1 == calculate_net_short_positions([con(exp3dte, 69, "P", -1)], "P")

    assert 1 == calculate_net_short_positions(
        [con(exp3dte, 69, "P", -1), con(exp3dte, 69, "C", 1)], "P"
    )

    assert 0 == calculate_net_short_positions(
        [con(exp3dte, 69, "P", -1), con(exp3dte, 69, "C", 1)], "C"
    )

    assert 0 == calculate_net_short_positions(
        [con(exp3dte, 69, "C", -1), con(exp3dte, 69, "C", 1)], "C"
    )

    assert 0 == calculate_net_short_positions(
        [
            con(exp3dte, 69, "C", -1),
            con(exp3dte, 69, "C", 1),
            con(exp30dte, 69, "C", 1),
        ],
        "C",
    )

    assert 0 == calculate_net_short_positions(
        [
            con(exp3dte, 69, "C", -1),
            con(exp3dte, 69, "P", -1),
            con(exp3dte, 69, "C", 1),
            con(exp30dte, 69, "C", 1),
        ],
        "C",
    )

    assert 0 == calculate_net_short_positions(
        [
            con(exp3dte, 69, "C", -1),
            con(exp3dte, 69, "P", -1),
            con(exp3dte, 69, "C", 1),
            con(exp30dte, 70, "C", 1),
        ],
        "C",
    )

    assert 1 == calculate_net_short_positions(
        [
            con(exp3dte, 69, "C", -1),
            con(exp3dte, 69, "C", -1),
            con(exp3dte, 69, "C", 1),
            con(exp30dte, 70, "C", 1),
        ],
        "C",
    )

    assert 2 == calculate_net_short_positions(
        [
            con(exp3dte, 69, "C", -1),
            con(exp3dte, 69, "C", -1),
            con(exp3dte, 69, "P", 1),
            con(exp30dte, 69, "P", 1),
        ],
        "C",
    )

    assert 0 == calculate_net_short_positions(
        [
            con(exp3dte, 69, "C", -1),
            con(exp3dte, 69, "C", -1),
            con(exp3dte, 69, "C", 1),
            con(exp30dte, 69, "C", 5),
        ],
        "C",
    )

    assert 0 == calculate_net_short_positions(
        [
            con(exp3dte, 69, "C", -1),
            con(exp30dte, 69, "C", -1),
            con(exp3dte, 69, "C", 1),
            con(exp30dte, 69, "C", 5),
        ],
        "C",
    )

    assert 0 == calculate_net_short_positions(
        [
            con(exp3dte, 69, "P", -1),
            con(exp30dte, 69, "P", -1),
            con(exp3dte, 69, "P", 1),
            con(exp30dte, 69, "P", 5),
        ],
        "P",
    )

    assert 0 == calculate_net_short_positions(
        [
            con(exp3dte, 70, "P", -1),
            con(exp30dte, 69, "P", -1),
            con(exp3dte, 69, "P", 1),
            con(exp30dte, 70, "P", 5),
        ],
        "P",
    )

    assert 2 == calculate_net_short_positions(
        [
            con(exp3dte, 70, "P", -1),
            con(exp30dte, 69, "P", -1),
            con(exp3dte, 69, "P", 1),
            con(exp30dte, 68, "P", 5),
        ],
        "P",
    )

    assert 0 == calculate_net_short_positions(
        [
            con(exp3dte, 70, "C", -1),
            con(exp30dte, 69, "C", -1),
            con(exp3dte, 69, "C", 1),
            con(exp30dte, 68, "C", 5),
        ],
        "C",
    )

    assert 1 == calculate_net_short_positions(
        [
            con(exp3dte, 70, "C", -1),
            con(exp30dte, 69, "C", -1),
            con(exp3dte, 71, "C", 1),
            con(exp30dte, 70, "C", 5),
        ],
        "C",
    )

    assert 2 == calculate_net_short_positions(
        [
            con(exp3dte, 70, "C", -1),
            con(exp30dte, 71, "C", -1),
            con(exp3dte, 71, "C", 1),
            con(exp30dte, 72, "C", 5),
        ],
        "C",
    )

    assert 3 == calculate_net_short_positions(
        [
            con(exp3dte, 70, "C", -1),
            con(exp30dte, 71, "C", -1),
            con(exp90dte, 72, "C", -1),
            con(exp3dte, 71, "C", 1),
            con(exp30dte, 72, "C", 5),
        ],
        "C",
    )

    assert 5 == calculate_net_short_positions(
        [
            con(exp3dte, 60, "P", -10),
            con(exp30dte, 69, "P", -1),
            con(exp90dte, 69, "P", 1),
            con(exp90dte, 68, "P", 5),
        ],
        "P",
    )

    assert 10 == calculate_net_short_positions(
        [
            con(exp3dte, 70, "P", -10),
            con(exp30dte, 69, "P", -1),
            con(exp90dte, 69, "P", 1),
            con(exp90dte, 68, "P", 5),
        ],
        "P",
    )

    assert 0 == calculate_net_short_positions(
        [
            con(exp3dte, 60, "P", -10),
            con(exp30dte, 69, "P", -1),
            con(exp90dte, 69, "P", 1),
            con(exp90dte, 68, "P", 50),
        ],
        "P",
    )

    # A couple real-world examples
    exp9dte = (today + timedelta(days=9)).strftime("%Y%m%d")
    exp16dte = (today + timedelta(days=16)).strftime("%Y%m%d")
    exp23dte = (today + timedelta(days=23)).strftime("%Y%m%d")
    exp30dte = (today + timedelta(days=30)).strftime("%Y%m%d")
    exp37dte = (today + timedelta(days=37)).strftime("%Y%m%d")
    exp268dte = (today + timedelta(days=268)).strftime("%Y%m%d")

    assert 2 == calculate_net_short_positions(
        [
            con(exp9dte, 77.0, "P", -2),
            con(exp16dte, 76.0, "P", -1),
            con(exp16dte, 77.0, "P", -1),
            con(exp23dte, 77.0, "P", -6),
            con(exp30dte, 77.0, "P", -2),
            con(exp37dte, 77.0, "P", -5),
            con(exp268dte, 77.0, "P", 15),
        ],
        "P",
    )

    assert 0 == calculate_net_short_positions(
        [
            con(exp9dte, 77.0, "P", -2),
            con(exp16dte, 76.0, "P", -1),
            con(exp16dte, 77.0, "P", -1),
            con(exp23dte, 77.0, "P", -6),
            con(exp30dte, 77.0, "P", -2),
            con(exp37dte, 77.0, "P", -5),
            con(exp268dte, 77.0, "P", 15),
        ],
        "C",
    )

    assert 20 == calculate_net_short_positions(
        [
            con(exp23dte, 72.0, "C", -8),
            con(exp30dte, 66.0, "C", -8),
            con(exp30dte, 68.0, "C", -9),
            con(exp30dte, 69.0, "C", -7),
            con(exp30dte, 72.0, "C", -1),
            con(exp37dte, 59.5, "C", -8),
            con(exp37dte, 68.0, "C", -7),
            con(exp268dte, 55.0, "C", 5),
            con(exp268dte, 60.0, "C", 23),
        ],
        "C",
    )

    assert 0 == calculate_net_short_positions(
        [
            con(exp23dte, 72.0, "C", -8),
            con(exp30dte, 66.0, "C", -8),
            con(exp30dte, 68.0, "C", -9),
            con(exp30dte, 69.0, "C", -7),
            con(exp30dte, 72.0, "C", -1),
            con(exp37dte, 59.5, "C", -8),
            con(exp37dte, 68.0, "C", -7),
            con(exp268dte, 55.0, "C", 5),
            con(exp268dte, 60.0, "C", 23),
        ],
        "P",
    )


def test_weighted_avg_strike() -> None:
    today = date.today()
    exp3dte = (today + timedelta(days=3)).strftime("%Y%m%d")
    exp30dte = (today + timedelta(days=30)).strftime("%Y%m%d")
    exp90dte = (today + timedelta(days=90)).strftime("%Y%m%d")

    assert math.isclose(
        70,
        weighted_avg_short_strike(
            [
                con(exp3dte, 70, "C", -1),
                con(exp30dte, 70, "C", -1),
                con(exp90dte, 70, "C", -1),
                con(exp3dte, 100, "C", 1),
                con(exp30dte, 100, "C", 5),
            ],
            "C",
        )
        or -1,
    )
    assert math.isclose(
        100,
        weighted_avg_long_strike(
            [
                con(exp3dte, 70, "C", -1),
                con(exp30dte, 70, "C", -1),
                con(exp90dte, 70, "C", -1),
                con(exp3dte, 100, "C", 1),
                con(exp30dte, 100, "C", 5),
            ],
            "C",
        )
        or -1,
    )
    assert math.isclose(
        70,
        weighted_avg_short_strike(
            [
                con(exp3dte, 70, "P", -1),
                con(exp30dte, 70, "P", -1),
                con(exp90dte, 70, "P", -1),
                con(exp3dte, 100, "P", 1),
                con(exp30dte, 100, "P", 5),
            ],
            "P",
        )
        or -1,
    )
    assert math.isclose(
        100,
        weighted_avg_long_strike(
            [
                con(exp3dte, 70, "P", -1),
                con(exp30dte, 70, "P", -1),
                con(exp90dte, 70, "P", -1),
                con(exp3dte, 100, "P", 1),
                con(exp30dte, 100, "P", 5),
            ],
            "P",
        )
        or -1,
    )

    assert math.isclose(
        28,
        weighted_avg_short_strike(
            [
                con(exp3dte, 10, "P", -4),
                con(exp3dte, 100, "P", -1),
                con(exp3dte, 100, "P", 4),
                con(exp3dte, 10, "P", 1),
            ],
            "P",
        )
        or -1,
    )

    assert math.isclose(
        82,
        weighted_avg_long_strike(
            [
                con(exp3dte, 10, "P", -4),
                con(exp3dte, 100, "P", -1),
                con(exp3dte, 100, "P", 4),
                con(exp3dte, 10, "P", 1),
            ],
            "P",
        )
        or -1,
    )


def test_would_increase_spread() -> None:
    # Test BUY order with lmtPrice < 0 and updated_price > lmtPrice
    order1 = Order(action="BUY", lmtPrice=-10)
    updated_price1 = -5.0
    assert would_increase_spread(order1, updated_price1) is False

    # Test BUY order with lmtPrice < 0 and updated_price < lmtPrice
    order2 = Order(action="BUY", lmtPrice=-10)
    updated_price2 = -15.0
    assert would_increase_spread(order2, updated_price2) is True

    # Test BUY order with lmtPrice > 0 and updated_price < lmtPrice
    order3 = Order(action="BUY", lmtPrice=10)
    updated_price3 = 5.0
    assert would_increase_spread(order3, updated_price3) is True

    # Test BUY order with lmtPrice > 0 and updated_price > lmtPrice
    order4 = Order(action="BUY", lmtPrice=10)
    updated_price4 = 15.0
    assert would_increase_spread(order4, updated_price4) is False

    # Test SELL order with lmtPrice < 0 and updated_price < lmtPrice
    order5 = Order(action="SELL", lmtPrice=-10)
    updated_price5 = -15.0
    assert would_increase_spread(order5, updated_price5) is False

    # Test SELL order with lmtPrice < 0 and updated_price > lmtPrice
    order6 = Order(action="SELL", lmtPrice=-10)
    updated_price6 = -5.0
    assert would_increase_spread(order6, updated_price6) is True

    # Test SELL order with lmtPrice > 0 and updated_price > lmtPrice
    order7 = Order(action="SELL", lmtPrice=10)
    updated_price7 = 15.0
    assert would_increase_spread(order7, updated_price7) is True

    # Test SELL order with lmtPrice > 0 and updated_price < lmtPrice
    order8 = Order(action="SELL", lmtPrice=10)
    updated_price8 = 5.0
    assert would_increase_spread(order8, updated_price8) is False
