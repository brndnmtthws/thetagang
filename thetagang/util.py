from __future__ import annotations

from typing import Dict, Iterable

from ib_async import AccountValue, PortfolioItem


def account_summary_to_dict(
    account_summary: Iterable[AccountValue],
) -> Dict[str, AccountValue]:
    """Convert an iterable of :class:`AccountValue` objects to a dictionary."""

    return {value.tag: value for value in account_summary}


def position_pnl(position: PortfolioItem) -> float:
    """Return the unrealized P&L as a fraction of the position's cost basis."""

    cost_basis = float(position.averageCost) * float(position.position)
    if cost_basis == 0:
        return 0.0
    return float(position.unrealizedPNL) / abs(cost_basis)
