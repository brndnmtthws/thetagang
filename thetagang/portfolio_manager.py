from __future__ import annotations

from typing import Iterable, Optional

from ib_async import AccountValue, IB, PortfolioItem
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from thetagang.config import Config
from thetagang.fmt import dfmt, ffmt, ifmt, pfmt
from thetagang.util import account_summary_to_dict, position_pnl


class PortfolioManager:
    """Render account and position information for the configured account."""

    def __init__(
        self,
        config: Config,
        ib: IB,
        console: Optional[Console] = None,
    ) -> None:
        self.config = config
        self.ib = ib
        self.console = console or Console()

    async def run(self) -> None:
        """Fetch account data and render it to the console."""

        summary_raw = await self.ib.accountSummaryAsync(self.config.account.number)
        summary = account_summary_to_dict(summary_raw)

        if "NetLiquidation" not in summary:
            raise RuntimeError(
                f"Account number {self.config.account.number} appears invalid "
                "(no account data returned)."
            )

        summary_table = self.create_account_summary_table(summary)
        self.console.print(Panel(summary_table, title="Account summary", expand=False))

        positions = self.ib.portfolio(self.config.account.number)
        positions_table = self.create_positions_table(positions)

        if positions_table.row_count:
            self.console.print(
                Panel(positions_table, title="Open positions", expand=False)
            )
        else:
            self.console.print(Panel("No open positions", expand=False))

    @staticmethod
    def create_account_summary_table(
        account_summary: dict[str, AccountValue]
    ) -> Table:
        """Create a table showing key account metrics."""

        table = Table(show_header=False)
        table.add_row(
            "Net liquidation", dfmt(account_summary["NetLiquidation"].value, 0)
        )
        if "ExcessLiquidity" in account_summary:
            table.add_row(
                "Excess liquidity", dfmt(account_summary["ExcessLiquidity"].value, 0)
            )
        if "InitMarginReq" in account_summary:
            table.add_row(
                "Initial margin", dfmt(account_summary["InitMarginReq"].value, 0)
            )
        if "FullMaintMarginReq" in account_summary:
            table.add_row(
                "Maintenance margin",
                dfmt(account_summary["FullMaintMarginReq"].value, 0),
            )
        if "BuyingPower" in account_summary:
            table.add_row("Buying power", dfmt(account_summary["BuyingPower"].value, 0))
        if "TotalCashValue" in account_summary:
            table.add_row("Total cash", dfmt(account_summary["TotalCashValue"].value, 0))
        if "Cushion" in account_summary:
            table.add_row("Cushion", pfmt(account_summary["Cushion"].value, 2))
        return table

    @staticmethod
    def create_positions_table(positions: Iterable[PortfolioItem]) -> Table:
        """Create a table summarising portfolio positions."""

        table = Table(show_header=True)
        table.add_column("Symbol")
        table.add_column("Description")
        table.add_column("Position", justify="right")
        table.add_column("Mark", justify="right")
        table.add_column("Avg price", justify="right")
        table.add_column("Market value", justify="right")
        table.add_column("Unrealized P&L", justify="right")
        table.add_column("P&L %", justify="right")

        for position in sorted(
            positions,
            key=lambda p: (
                getattr(p.contract, "symbol", ""),
                getattr(p.contract, "localSymbol", ""),
            ),
        ):
            qty_display = PortfolioManager._format_quantity(position.position)
            pnl_percent = pfmt(position_pnl(position), 2)
            table.add_row(
                getattr(position.contract, "symbol", ""),
                getattr(position.contract, "localSymbol", ""),
                qty_display,
                dfmt(position.marketPrice),
                dfmt(position.averageCost),
                dfmt(position.marketValue, 0),
                dfmt(position.unrealizedPNL, 0),
                pnl_percent,
            )
        return table

    @staticmethod
    def _format_quantity(quantity: float) -> str:
        """Format a position quantity for display."""

        if float(quantity).is_integer():
            return ifmt(int(quantity))
        return ffmt(float(quantity), 4)
