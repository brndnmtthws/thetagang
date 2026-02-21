from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Protocol

from ib_async import AccountValue, PortfolioItem
from rich.console import Group
from rich.panel import Panel

from thetagang import log

AccountSummary = Dict[str, AccountValue]
PortfolioBySymbol = Dict[str, List[PortfolioItem]]


@dataclass
class OptionsStrategyDeps:
    enabled_stages: set[str]
    write_service: "OptionsWriteService"
    manage_service: "OptionsManageService"


class OptionsWriteService(Protocol):
    async def check_if_can_write_puts(
        self, account_summary: AccountSummary, portfolio_positions: PortfolioBySymbol
    ) -> Any: ...

    async def write_puts(self, puts_to_write: List[Any]) -> None: ...

    async def check_for_uncovered_positions(
        self, account_summary: AccountSummary, portfolio_positions: PortfolioBySymbol
    ) -> Any: ...

    async def write_calls(self, calls_to_write: List[Any]) -> None: ...


class OptionsManageService(Protocol):
    async def check_puts(self, portfolio_positions: PortfolioBySymbol) -> Any: ...

    async def check_calls(self, portfolio_positions: PortfolioBySymbol) -> Any: ...

    async def roll_puts(
        self, puts: List[Any], account_summary: AccountSummary
    ) -> List[Any]: ...

    async def roll_calls(
        self,
        calls: List[Any],
        account_summary: AccountSummary,
        portfolio_positions: PortfolioBySymbol,
    ) -> List[Any]: ...

    async def close_puts(self, puts: List[Any]) -> None: ...

    async def close_calls(self, calls: List[Any]) -> None: ...


async def run_option_write_stages(
    deps: OptionsStrategyDeps,
    account_summary: AccountSummary,
    portfolio_positions: PortfolioBySymbol,
    options_enabled: bool,
) -> None:
    if not options_enabled:
        log.notice(
            "Regime rebalancing shares-only enabled; skipping option writes and rolls."
        )
        return

    if "options_write_puts" in deps.enabled_stages:
        (
            positions_table,
            put_actions_table,
            puts_to_write,
        ) = await deps.write_service.check_if_can_write_puts(
            account_summary, portfolio_positions
        )
        log.print(positions_table)
        log.print(put_actions_table)
        await deps.write_service.write_puts(puts_to_write)

    if "options_write_calls" in deps.enabled_stages:
        (
            call_actions_table,
            calls_to_write,
        ) = await deps.write_service.check_for_uncovered_positions(
            account_summary, portfolio_positions
        )
        log.print(call_actions_table)
        await deps.write_service.write_calls(calls_to_write)


async def run_option_management_stages(
    deps: OptionsStrategyDeps,
    account_summary: AccountSummary,
    portfolio_positions: PortfolioBySymbol,
    options_enabled: bool,
) -> None:
    if not options_enabled:
        return

    should_roll = "options_roll_positions" in deps.enabled_stages
    should_close = "options_close_positions" in deps.enabled_stages
    if not (should_roll or should_close):
        return

    (rollable_puts, closeable_puts, group1) = await deps.manage_service.check_puts(
        portfolio_positions
    )
    (rollable_calls, closeable_calls, group2) = await deps.manage_service.check_calls(
        portfolio_positions
    )
    log.print(Panel(Group(group1, group2)))

    if should_close:
        puts_to_close = closeable_puts
        calls_to_close = closeable_calls
        if should_roll:
            puts_to_close += await deps.manage_service.roll_puts(
                rollable_puts, account_summary
            )
            calls_to_close += await deps.manage_service.roll_calls(
                rollable_calls, account_summary, portfolio_positions
            )
        await deps.manage_service.close_puts(puts_to_close)
        await deps.manage_service.close_calls(calls_to_close)
    elif should_roll:
        await deps.manage_service.roll_puts(rollable_puts, account_summary)
        await deps.manage_service.roll_calls(
            rollable_calls, account_summary, portfolio_positions
        )
