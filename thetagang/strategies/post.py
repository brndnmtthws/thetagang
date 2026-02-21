from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Protocol

from ib_async import AccountValue, PortfolioItem

AccountSummary = Dict[str, AccountValue]
PortfolioBySymbol = Dict[str, List[PortfolioItem]]


@dataclass
class PostStrategyDeps:
    enabled_stages: set[str]
    service: "PostStageService"


class PostStageService(Protocol):
    async def do_vix_hedging(
        self, account_summary: AccountSummary, portfolio_positions: PortfolioBySymbol
    ) -> None: ...

    async def do_cashman(
        self, account_summary: AccountSummary, portfolio_positions: PortfolioBySymbol
    ) -> None: ...


async def run_post_stages(
    deps: PostStrategyDeps,
    account_summary: AccountSummary,
    portfolio_positions: PortfolioBySymbol,
) -> None:
    if "post_vix_call_hedge" in deps.enabled_stages:
        await deps.service.do_vix_hedging(account_summary, portfolio_positions)

    if "post_cash_management" in deps.enabled_stages:
        await deps.service.do_cashman(account_summary, portfolio_positions)
