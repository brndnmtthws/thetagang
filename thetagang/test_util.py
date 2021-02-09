from ib_insync import Option, PortfolioItem
from ib_insync.contract import Stock

from thetagang.util import get_target_delta, position_pnl


def test_position_pnl():
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


def test_get_delta():
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
