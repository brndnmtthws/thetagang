from ib_async import AccountValue, PortfolioItem
from ib_async.contract import Stock

from thetagang.util import account_summary_to_dict, position_pnl


def test_account_summary_to_dict() -> None:
    values = [
        AccountValue(
            account="DU123",
            tag="NetLiquidation",
            value="100000",
            currency="USD",
            modelCode="",
        ),
        AccountValue(
            account="DU123",
            tag="TotalCashValue",
            value="20000",
            currency="USD",
            modelCode="",
        ),
    ]

    summary = account_summary_to_dict(values)

    assert summary["NetLiquidation"].value == "100000"
    assert summary["TotalCashValue"].value == "20000"


def test_position_pnl_handles_cost_basis() -> None:
    position = PortfolioItem(
        account="DU123",
        contract=Stock("SPY", "SMART", "USD"),
        position=10.0,
        marketPrice=400.0,
        marketValue=4000.0,
        averageCost=350.0,
        unrealizedPNL=500.0,
        realizedPNL=0.0,
    )

    assert round(position_pnl(position), 4) == 0.1429


def test_position_pnl_zero_cost_basis() -> None:
    position = PortfolioItem(
        account="DU123",
        contract=Stock("SPY", "SMART", "USD"),
        position=0.0,
        marketPrice=0.0,
        marketValue=0.0,
        averageCost=0.0,
        unrealizedPNL=0.0,
        realizedPNL=0.0,
    )

    assert position_pnl(position) == 0.0
