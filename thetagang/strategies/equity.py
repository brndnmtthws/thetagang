from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Protocol

from ib_async import AccountValue, PortfolioItem

from thetagang import log

AccountSummary = Dict[str, AccountValue]
PortfolioBySymbol = Dict[str, List[PortfolioItem]]


@dataclass
class EquityStrategyDeps:
    enabled_stages: set[str]
    regime_rebalance_enabled: bool
    regime_service: "RegimeRebalanceService"
    rebalance_service: "EquityRebalanceService"


class RegimeRebalanceService(Protocol):
    async def check_regime_rebalance_positions(
        self, account_summary: AccountSummary, portfolio_positions: PortfolioBySymbol
    ) -> Any: ...

    async def execute_regime_rebalance_orders(self, orders: List[Any]) -> None: ...


class EquityRebalanceService(Protocol):
    async def check_buy_only_positions(
        self, account_summary: AccountSummary, portfolio_positions: PortfolioBySymbol
    ) -> Any: ...

    async def execute_buy_orders(self, orders: List[Any]) -> None: ...

    async def check_sell_only_positions(
        self, account_summary: AccountSummary, portfolio_positions: PortfolioBySymbol
    ) -> Any: ...

    async def execute_sell_orders(self, orders: List[Any]) -> None: ...


async def run_equity_rebalance_stages(
    deps: EquityStrategyDeps,
    account_summary: AccountSummary,
    portfolio_positions: PortfolioBySymbol,
) -> None:
    if "equity_regime_rebalance" in deps.enabled_stages:
        (
            regime_actions_table,
            regime_orders,
        ) = await deps.regime_service.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )
        if deps.regime_rebalance_enabled:
            log.print(regime_actions_table)
        if regime_orders:
            await deps.regime_service.execute_regime_rebalance_orders(regime_orders)

    if "equity_buy_rebalance" in deps.enabled_stages:
        (
            buy_actions_table,
            stocks_to_buy,
        ) = await deps.rebalance_service.check_buy_only_positions(
            account_summary, portfolio_positions
        )
        if stocks_to_buy:
            log.print(buy_actions_table)
            await deps.rebalance_service.execute_buy_orders(stocks_to_buy)

    if "equity_sell_rebalance" in deps.enabled_stages:
        (
            sell_actions_table,
            stocks_to_sell,
        ) = await deps.rebalance_service.check_sell_only_positions(
            account_summary, portfolio_positions
        )
        if stocks_to_sell:
            log.print(sell_actions_table)
            await deps.rebalance_service.execute_sell_orders(stocks_to_sell)
