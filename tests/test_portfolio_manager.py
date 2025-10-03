from ib_async import AccountValue, PortfolioItem
from ib_async.contract import Option, Stock

from thetagang.config import Config
from thetagang.portfolio_manager import PortfolioManager


def make_config() -> Config:
    return Config.from_dict({"account": {"number": "DU123"}})


def test_create_account_summary_table_includes_values() -> None:
    summary = {
        "NetLiquidation": AccountValue(
            account="DU123",
            tag="NetLiquidation",
            value="100000",
            currency="USD",
            modelCode="",
        ),
        "ExcessLiquidity": AccountValue(
            account="DU123",
            tag="ExcessLiquidity",
            value="50000",
            currency="USD",
            modelCode="",
        ),
        "Cushion": AccountValue(
            account="DU123",
            tag="Cushion",
            value="0.25",
            currency="USD",
            modelCode="",
        ),
    }

    table = PortfolioManager.create_account_summary_table(summary)

    assert table.row_count == 3
    labels = list(table.columns[0]._cells)
    assert "Net liquidation" in labels
    assert "Excess liquidity" in labels
    assert "Cushion" in labels


def test_create_positions_table_orders_by_symbol() -> None:
    positions = [
        PortfolioItem(
            account="DU123",
            contract=Option(
                conId=1,
                symbol="SPY",
                lastTradeDateOrContractMonth="20240119",
                strike=400.0,
                right="C",
                multiplier="100",
                exchange="SMART",
                currency="USD",
                localSymbol="SPY   240119C00400000",
                tradingClass="SPY",
            ),
            position=-1.0,
            marketPrice=1.5,
            marketValue=-150.0,
            averageCost=2.0,
            unrealizedPNL=50.0,
            realizedPNL=0.0,
        ),
        PortfolioItem(
            account="DU123",
            contract=Stock(
                conId=2,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                localSymbol="AAPL",
                primaryExchange="NASDAQ",
                tradingClass="AAPL",
            ),
            position=5.0,
            marketPrice=190.0,
            marketValue=950.0,
            averageCost=180.0,
            unrealizedPNL=50.0,
            realizedPNL=0.0,
        ),
    ]

    table = PortfolioManager.create_positions_table(positions)

    assert table.row_count == 2
    first_symbol = table.columns[0]._cells[0]
    assert first_symbol == "AAPL"


def test_format_quantity_handles_fractional() -> None:
    assert PortfolioManager._format_quantity(2.0) == "[green]2[/green]"
    assert PortfolioManager._format_quantity(1.25) == "[green]1.2500[/green]"
