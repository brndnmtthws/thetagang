from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List

from ib_async import AccountValue, Ticker


def resolve_symbol_configs(config: Any, *, context: str) -> Dict[str, Any]:
    symbols = getattr(config, "symbols", None)
    if isinstance(symbols, dict):
        return symbols

    portfolio = getattr(config, "portfolio", None)
    portfolio_symbols = getattr(portfolio, "symbols", None)
    if isinstance(portfolio_symbols, dict):
        return portfolio_symbols

    raise ValueError(
        f"{context}: expected config.symbols or config.portfolio.symbols to be a dict"
    )


@dataclass(frozen=True)
class OptionsRuntimeServiceAdapter:
    get_symbols_fn: Callable[[], List[str]]
    get_primary_exchange_fn: Callable[[str], str]
    get_buying_power_fn: Callable[[Dict[str, AccountValue]], int]
    get_maximum_new_contracts_for_fn: Callable[
        [str, str, Dict[str, AccountValue]], Awaitable[int]
    ]
    get_write_threshold_fn: Callable[[Ticker, str], Awaitable[tuple[float, float]]]
    get_close_price_fn: Callable[[Ticker], float]

    def get_symbols(self) -> List[str]:
        return self.get_symbols_fn()

    def get_primary_exchange(self, symbol: str) -> str:
        return self.get_primary_exchange_fn(symbol)

    def get_buying_power(self, account_summary: Dict[str, AccountValue]) -> int:
        return self.get_buying_power_fn(account_summary)

    async def get_maximum_new_contracts_for(
        self,
        symbol: str,
        primary_exchange: str,
        account_summary: Dict[str, AccountValue],
    ) -> int:
        return await self.get_maximum_new_contracts_for_fn(
            symbol, primary_exchange, account_summary
        )

    async def get_write_threshold(
        self, ticker: Ticker, right: str
    ) -> tuple[float, float]:
        return await self.get_write_threshold_fn(ticker, right)

    def get_close_price(self, ticker: Ticker) -> float:
        return self.get_close_price_fn(ticker)


@dataclass(frozen=True)
class EquityRuntimeServiceAdapter:
    get_primary_exchange_fn: Callable[[str], str]
    get_buying_power_fn: Callable[[Dict[str, AccountValue]], int]
    midpoint_or_market_price_fn: Callable[[Ticker], float]

    def get_primary_exchange(self, symbol: str) -> str:
        return self.get_primary_exchange_fn(symbol)

    def get_buying_power(self, account_summary: Dict[str, AccountValue]) -> int:
        return self.get_buying_power_fn(account_summary)

    def midpoint_or_market_price(self, ticker: Ticker) -> float:
        return self.midpoint_or_market_price_fn(ticker)
