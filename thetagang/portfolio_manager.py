import asyncio
import logging
import math
import random
from asyncio import Future
from datetime import date, datetime
from typing import Any, Coroutine, Dict, List, Optional, Tuple, cast

import numpy as np
from ib_async import (
    AccountValue,
    PortfolioItem,
    Ticker,
    util,
)
from ib_async.contract import Contract, Option, Stock
from ib_async.ib import IB
from ib_async.order import LimitOrder
from rich.console import Group
from rich.panel import Panel
from rich.table import Table

from thetagang import log
from thetagang.config import (
    CANONICAL_STAGE_ORDER,
    DEFAULT_RUN_STRATEGIES,
    Config,
    RunConfig,
    enabled_stage_ids_from_run,
    stage_enabled_map_from_run,
)
from thetagang.db import DataStore
from thetagang.fmt import dfmt, ffmt, ifmt, pfmt
from thetagang.ibkr import (
    IBKR,
    IBKRRequestTimeout,
    RequiredFieldValidationError,
    TickerField,
)
from thetagang.orders import Orders
from thetagang.strategies import (
    EquityStrategyDeps,
    OptionsStrategyDeps,
    PostStrategyDeps,
    run_equity_rebalance_stages,
    run_option_management_stages,
    run_option_write_stages,
    run_post_stages,
)
from thetagang.strategies.equity import EquityRebalanceService, RegimeRebalanceService
from thetagang.strategies.equity_engine import EquityRebalanceEngine
from thetagang.strategies.options import OptionsManageService, OptionsWriteService
from thetagang.strategies.options_engine import OptionsStrategyEngine
from thetagang.strategies.post_engine import PostStrategyEngine
from thetagang.strategies.regime_engine import RegimeRebalanceEngine
from thetagang.strategies.runtime_services import (
    EquityRuntimeServiceAdapter,
    OptionsRuntimeServiceAdapter,
    resolve_symbol_configs,
)
from thetagang.trades import Trades
from thetagang.trading_operations import (
    OptionChainScanner,
    OrderOperations,
)
from thetagang.util import (
    account_summary_to_dict,
    get_short_positions,
    midpoint_or_market_price,
    portfolio_positions_to_dict,
    position_pnl,
    would_increase_spread,
)

from .options import option_dte

# Turn off some of the more annoying logging output from ib_async
logging.getLogger("ib_async.ib").setLevel(logging.ERROR)
logging.getLogger("ib_async.wrapper").setLevel(logging.CRITICAL)


class PortfolioManager:
    @staticmethod
    def get_close_price(ticker: Ticker) -> float:
        """Get the close price from ticker, falling back to market price if close is NaN.

        This handles the ib_async v2.0.1 change where ticker.close defaults to NaN.
        """
        return ticker.close if not util.isNan(ticker.close) else ticker.marketPrice()

    def __init__(
        self,
        config: Config,
        ib: IB,
        completion_future: Future[bool],
        dry_run: bool,
        data_store: Optional[DataStore] = None,
        run_stage_flags: Optional[Dict[str, bool]] = None,
        run_stage_order: Optional[List[str]] = None,
    ) -> None:
        self.account_number = config.runtime.account.number
        self.config = config
        self.data_store = data_store
        self.ibkr = IBKR(
            ib,
            config.runtime.ib_async.api_response_wait_time,
            config.runtime.orders.exchange,
            data_store=data_store,
        )
        self.completion_future = completion_future
        self.has_excess_calls: set[str] = set()
        self.has_excess_puts: set[str] = set()
        self.orders: Orders = Orders()
        self.trades: Trades = Trades(self.ibkr, data_store=data_store)
        self.target_quantities: Dict[str, int] = {}
        self.qualified_contracts: Dict[int, Contract] = {}
        self.dry_run = dry_run
        self.last_untracked_positions: Dict[str, List[PortfolioItem]] = {}
        self.order_ops = OrderOperations(
            config=self.config,
            account_number=self.account_number,
            orders=self.orders,
            data_store=self.data_store,
        )
        self.options_runtime_services = OptionsRuntimeServiceAdapter(
            get_symbols_fn=lambda: self.get_symbols(),
            get_primary_exchange_fn=lambda symbol: self.get_primary_exchange(symbol),
            get_buying_power_fn=lambda account_summary: self.get_buying_power(
                account_summary
            ),
            get_maximum_new_contracts_for_fn=(
                lambda symbol, primary_exchange, account_summary: (
                    self.get_maximum_new_contracts_for(
                        symbol, primary_exchange, account_summary
                    )
                )
            ),
            get_write_threshold_fn=lambda ticker, right: self.get_write_threshold(
                ticker, right
            ),
            get_close_price_fn=lambda ticker: self.get_close_price(ticker),
        )
        self.equity_runtime_services = EquityRuntimeServiceAdapter(
            get_primary_exchange_fn=lambda symbol: self.get_primary_exchange(symbol),
            get_buying_power_fn=lambda account_summary: self.get_buying_power(
                account_summary
            ),
            midpoint_or_market_price_fn=lambda ticker: self.midpoint_or_market_price(
                ticker
            ),
        )
        self.option_scanner = OptionChainScanner(
            config=self.config, ibkr=self.ibkr, order_ops=self.order_ops
        )
        self.options_engine = OptionsStrategyEngine(
            config=self.config,
            ibkr=self.ibkr,
            option_scanner=self.option_scanner,
            order_ops=self.order_ops,
            services=self.options_runtime_services,
            target_quantities=self.target_quantities,
            has_excess_puts=self.has_excess_puts,
            has_excess_calls=self.has_excess_calls,
            qualified_contracts=self.qualified_contracts,
        )
        self.regime_engine = RegimeRebalanceEngine(
            config=self.config,
            ibkr=self.ibkr,
            order_ops=self.order_ops,
            data_store=self.data_store,
            get_primary_exchange=self.get_primary_exchange,
            get_buying_power=self.get_regime_buying_power,
            now_provider=lambda: datetime.now(),
        )
        self.equity_engine = EquityRebalanceEngine(
            config=self.config,
            ibkr=self.ibkr,
            order_ops=self.order_ops,
            services=self.equity_runtime_services,
            regime_engine=self.regime_engine,
        )
        self.post_engine = PostStrategyEngine(
            config=self.config,
            ibkr=self.ibkr,
            order_ops=self.order_ops,
            option_scanner=self.option_scanner,
            orders=self.orders,
            qualified_contracts=self.qualified_contracts,
        )
        if run_stage_flags is None:
            default_run = RunConfig(strategies=DEFAULT_RUN_STRATEGIES)
            self.run_stage_flags = stage_enabled_map_from_run(default_run)
            self.run_stage_order = enabled_stage_ids_from_run(default_run)
        else:
            self.run_stage_flags = dict(run_stage_flags)
            self.run_stage_order = [
                stage_id
                for stage_id in CANONICAL_STAGE_ORDER
                if self.run_stage_flags.get(stage_id, False)
            ]
        if run_stage_order is not None:
            self.run_stage_order = list(run_stage_order)
            enabled_set = set(self.run_stage_order)
            self.run_stage_flags = {
                stage_id: (stage_id in enabled_set)
                for stage_id in CANONICAL_STAGE_ORDER
            }

    def stage_enabled(self, stage_id: str) -> bool:
        return bool(self.run_stage_flags.get(stage_id, False))

    def _options_strategy_deps(self, enabled_stages: set[str]) -> OptionsStrategyDeps:
        return OptionsStrategyDeps(
            enabled_stages=enabled_stages,
            write_service=cast(OptionsWriteService, self.options_engine),
            manage_service=cast(OptionsManageService, self.options_engine),
        )

    def _sync_options_engine_state(self) -> None:
        self.options_engine.target_quantities = self.target_quantities
        self.options_engine.has_excess_puts = self.has_excess_puts
        self.options_engine.has_excess_calls = self.has_excess_calls

    def _equity_strategy_deps(self, enabled_stages: set[str]) -> EquityStrategyDeps:
        return EquityStrategyDeps(
            enabled_stages=enabled_stages,
            regime_rebalance_enabled=bool(
                self.config.strategies.regime_rebalance.enabled
            ),
            regime_service=cast(RegimeRebalanceService, self.equity_engine),
            rebalance_service=cast(EquityRebalanceService, self.equity_engine),
        )

    def _post_strategy_deps(self, enabled_stages: set[str]) -> PostStrategyDeps:
        return PostStrategyDeps(
            enabled_stages=enabled_stages,
            service=self.post_engine,
        )

    def get_short_calls(
        self, portfolio_positions: Dict[str, List[PortfolioItem]]
    ) -> List[PortfolioItem]:
        return self.get_short_contracts(portfolio_positions, "C")

    def get_short_puts(
        self, portfolio_positions: Dict[str, List[PortfolioItem]]
    ) -> List[PortfolioItem]:
        return self.get_short_contracts(portfolio_positions, "P")

    def _regime_rebalance_symbols(self) -> set[str]:
        regime_rebalance = self.config.strategies.regime_rebalance
        if not regime_rebalance.enabled:
            return set()
        return set(regime_rebalance.symbols)

    def options_trading_enabled(self) -> bool:
        regime_rebalance = self.config.strategies.regime_rebalance
        return not (regime_rebalance.enabled and regime_rebalance.shares_only)

    def get_short_contracts(
        self, portfolio_positions: Dict[str, List[PortfolioItem]], right: str
    ) -> List[PortfolioItem]:
        ret: List[PortfolioItem] = []
        for symbol in portfolio_positions:
            ret = ret + get_short_positions(portfolio_positions[symbol], right)
        return ret

    async def put_is_itm(self, contract: Contract) -> bool:
        return await self.options_engine.put_is_itm(contract)

    def position_can_be_closed(self, position: PortfolioItem, table: Table) -> bool:
        return self.options_engine.position_can_be_closed(position, table)

    def put_can_be_closed(self, put: PortfolioItem, table: Table) -> bool:
        return self.options_engine.put_can_be_closed(put, table)

    async def put_can_be_rolled(self, put: PortfolioItem, table: Table) -> bool:
        return await self.options_engine.put_can_be_rolled(put, table)

    async def call_is_itm(self, contract: Contract) -> bool:
        return await self.options_engine.call_is_itm(contract)

    def call_can_be_closed(self, call: PortfolioItem, table: Table) -> bool:
        return self.options_engine.call_can_be_closed(call, table)

    async def call_can_be_rolled(self, call: PortfolioItem, table: Table) -> bool:
        return await self.options_engine.call_can_be_rolled(call, table)

    def get_symbols(self) -> List[str]:
        return list(self.config.portfolio.symbols.keys())

    def filter_positions(
        self, portfolio_positions: List[PortfolioItem]
    ) -> List[PortfolioItem]:
        filtered_positions, _ = self.partition_positions(portfolio_positions)
        return filtered_positions

    def partition_positions(
        self, portfolio_positions: List[PortfolioItem]
    ) -> Tuple[List[PortfolioItem], List[PortfolioItem]]:
        symbols = self.get_symbols()
        tracked_positions: List[PortfolioItem] = []
        untracked_positions: List[PortfolioItem] = []
        for item in portfolio_positions:
            if item.account != self.account_number or item.position == 0:
                continue
            if (
                item.contract.symbol in symbols
                or item.contract.symbol == "VIX"
                or item.contract.symbol
                == self.config.strategies.cash_management.cash_fund
            ):
                tracked_positions.append(item)
            else:
                untracked_positions.append(item)
        return (tracked_positions, untracked_positions)

    async def get_portfolio_positions(self) -> Dict[str, List[PortfolioItem]]:
        attempts = 3
        symbols = set(self.get_symbols())
        self.last_untracked_positions = {}

        for attempt in range(1, attempts + 1):
            try:
                await self.ibkr.refresh_account_updates(self.account_number)
            except IBKRRequestTimeout as exc:
                if attempt == attempts:
                    log.warning(
                        (
                            f"Attempt {attempt}/{attempts}: {exc}. "
                            "Proceeding without a fresh account update snapshot."
                        )
                    )
                else:
                    log.warning(
                        f"Attempt {attempt}/{attempts}: {exc}. Retrying account update request..."
                    )
                    await asyncio.sleep(1)
                    continue

            portfolio_positions = self.ibkr.portfolio(account=self.account_number)
            filtered_positions, untracked_positions = self.partition_positions(
                portfolio_positions
            )
            portfolio_by_symbol = portfolio_positions_to_dict(filtered_positions)
            self.last_untracked_positions = portfolio_positions_to_dict(
                untracked_positions
            )
            filtered_conids = {item.contract.conId for item in filtered_positions}

            if portfolio_by_symbol:
                # Still verify against the latest positions snapshot to ensure we didn't
                # lose any holdings in the portfolio view.
                try:
                    positions_snapshot = await self.ibkr.refresh_positions()
                except IBKRRequestTimeout as exc:
                    log.warning(
                        f"Attempt {attempt}/{attempts}: {exc}. Retrying positions snapshot request..."
                    )
                    if attempt == attempts:
                        raise
                    await asyncio.sleep(1)
                    continue

                tracked_positions = [
                    pos
                    for pos in positions_snapshot
                    if pos.account == self.account_number
                    and (
                        pos.contract.symbol in symbols
                        or pos.contract.symbol == "VIX"
                        or pos.contract.symbol
                        == self.config.strategies.cash_management.cash_fund
                    )
                    and pos.position != 0
                ]
                missing_positions = [
                    pos
                    for pos in tracked_positions
                    if pos.contract.conId not in filtered_conids
                ]

                if not missing_positions:
                    return portfolio_by_symbol

                missing_symbols = ", ".join(
                    sorted({pos.contract.symbol for pos in missing_positions})
                )
                log.warning(
                    (
                        f"Attempt {attempt}/{attempts}: Portfolio snapshot is missing "
                        f"{len(missing_positions)} of {len(tracked_positions)} tracked "
                        f"positions (symbols: {missing_symbols}). Waiting briefly before retrying..."
                    )
                )
                await asyncio.sleep(1)
                continue

            try:
                positions_snapshot = await self.ibkr.refresh_positions()
            except IBKRRequestTimeout as exc:
                log.warning(
                    f"Attempt {attempt}/{attempts}: {exc}. Retrying positions snapshot request..."
                )
                if attempt == attempts:
                    raise
                await asyncio.sleep(1)
                continue

            tracked_positions = [
                pos
                for pos in positions_snapshot
                if pos.account == self.account_number
                and (
                    pos.contract.symbol in symbols
                    or pos.contract.symbol == "VIX"
                    or pos.contract.symbol
                    == self.config.strategies.cash_management.cash_fund
                )
                and pos.position != 0
            ]

            if not tracked_positions:
                return portfolio_by_symbol

            log.warning(
                (
                    f"Attempt {attempt}/{attempts}: IBKR reported {len(tracked_positions)} "
                    "tracked positions but returned an empty portfolio snapshot. "
                    "Waiting briefly before retrying..."
                )
            )
            await asyncio.sleep(1)

        raise RuntimeError(
            "Failed to load IBKR portfolio positions after multiple attempts. "
            "Aborting run to avoid trading on incomplete data."
        )

    def initialize_account(self) -> None:
        self.ibkr.set_market_data_type(self.config.runtime.account.market_data_type)

        if self.config.runtime.account.cancel_orders:
            # Cancel any existing orders
            open_trades = self.ibkr.open_trades()
            for trade in open_trades:
                if not trade.isDone() and (
                    trade.contract.symbol in self.get_symbols()
                    or (
                        self.config.strategies.vix_call_hedge.enabled
                        and trade.contract.symbol == "VIX"
                    )
                    or (
                        self.config.strategies.cash_management.enabled
                        and trade.contract.symbol
                        == self.config.strategies.cash_management.cash_fund
                    )
                ):
                    log.warning(
                        f"{trade.contract.symbol}: Canceling order {trade.order}"
                    )
                    self.ibkr.cancel_order(trade.order)

    async def summarize_account(
        self,
    ) -> Tuple[
        Dict[str, AccountValue],
        Dict[str, List[PortfolioItem]],
    ]:
        account_summary = await self.ibkr.account_summary(self.account_number)
        account_summary = account_summary_to_dict(account_summary)

        if "NetLiquidation" not in account_summary:
            raise RuntimeError(
                f"Account number {self.config.runtime.account.number} appears invalid (no account data returned)"
            )

        table = Table(title="Account summary")
        table.add_column("Item")
        table.add_column("Value", justify="right")
        table.add_row(
            "Net liquidation", dfmt(account_summary["NetLiquidation"].value, 0)
        )
        table.add_row(
            "Excess liquidity", dfmt(account_summary["ExcessLiquidity"].value, 0)
        )
        table.add_row("Initial margin", dfmt(account_summary["InitMarginReq"].value, 0))
        table.add_row(
            "Maintenance margin", dfmt(account_summary["FullMaintMarginReq"].value, 0)
        )
        table.add_row("Buying power", dfmt(account_summary["BuyingPower"].value, 0))
        table.add_row("Total cash", dfmt(account_summary["TotalCashValue"].value, 0))
        table.add_row("Cushion", pfmt(account_summary["Cushion"].value, 0))
        table.add_section()
        table.add_row(
            "Target buying power usage", dfmt(self.get_buying_power(account_summary), 0)
        )
        log.print(Panel(table))

        portfolio_positions = await self.get_portfolio_positions()
        untracked_positions = self.last_untracked_positions
        if self.data_store:
            self.data_store.record_account_snapshot(account_summary)
            combined_positions: Dict[str, List[PortfolioItem]] = dict(
                portfolio_positions
            )
            for symbol, positions in untracked_positions.items():
                if symbol in combined_positions:
                    combined_positions[symbol].extend(positions)
                else:
                    combined_positions[symbol] = positions
            self.data_store.record_positions_snapshot(combined_positions)

        position_values: Dict[int, Dict[str, str]] = {}

        async def is_itm(pos: PortfolioItem) -> str:
            if isinstance(pos.contract, Option):
                if pos.contract.right.startswith("C") and await self.call_is_itm(
                    pos.contract
                ):
                    return "✔️"
                if pos.contract.right.startswith("P") and await self.put_is_itm(
                    pos.contract
                ):
                    return "✔️"
            return ""

        async def load_position_task(pos: PortfolioItem) -> None:
            qty = pos.position
            if isinstance(qty, float):
                qty_display = ifmt(int(qty)) if qty.is_integer() else ffmt(qty, 4)
            else:
                qty_display = ifmt(int(qty))
            position_values[pos.contract.conId] = {
                "qty": qty_display,
                "mktprice": dfmt(pos.marketPrice),
                "avgprice": dfmt(pos.averageCost),
                "value": dfmt(pos.marketValue, 0),
                "cost": dfmt(pos.averageCost * pos.position, 0),
                "unrealized": dfmt(pos.unrealizedPNL, 0),
                "p&l": pfmt(position_pnl(pos), 1),
                "itm?": await is_itm(pos),
            }
            if isinstance(pos.contract, Option):
                position_values[pos.contract.conId]["avgprice"] = dfmt(
                    pos.averageCost / float(pos.contract.multiplier)
                )
                position_values[pos.contract.conId]["strike"] = dfmt(
                    pos.contract.strike
                )
                position_values[pos.contract.conId]["dte"] = str(
                    option_dte(pos.contract.lastTradeDateOrContractMonth)
                )
                position_values[pos.contract.conId]["exp"] = str(
                    pos.contract.lastTradeDateOrContractMonth
                )

        tasks: List[Coroutine[Any, Any, None]] = []
        for _, positions in portfolio_positions.items():
            for position in positions:
                tasks.append(load_position_task(position))
        for _, positions in untracked_positions.items():
            for position in positions:
                tasks.append(load_position_task(position))
        await log.track_async(tasks, "Loading portfolio positions...")

        table = Table(
            title="Portfolio positions",
            collapse_padding=True,
        )
        table.add_column("Symbol")
        table.add_column("R")
        table.add_column("Qty", justify="right")
        table.add_column("MktPrice", justify="right")
        table.add_column("AvgPrice", justify="right")
        table.add_column("Value", justify="right")
        table.add_column("Cost", justify="right")
        table.add_column("Unrealized P&L", justify="right")
        table.add_column("P&L", justify="right")
        table.add_column("Strike", justify="right")
        table.add_column("Exp", justify="right")
        table.add_column("DTE", justify="right")
        table.add_column("ITM?")

        def getval(col: str, conId: int) -> str:
            return position_values[conId][col]

        def add_symbol_positions(symbol: str, positions: List[PortfolioItem]) -> None:
            table.add_row(symbol)
            sorted_positions = sorted(
                positions,
                key=lambda p: (
                    option_dte(p.contract.lastTradeDateOrContractMonth)
                    if isinstance(p.contract, Option)
                    else -1
                ),  # Keep stonks on top
            )

            for pos in sorted_positions:
                conId = pos.contract.conId
                if isinstance(pos.contract, Stock):
                    table.add_row(
                        "",
                        "S",
                        getval("qty", conId),
                        getval("mktprice", conId),
                        getval("avgprice", conId),
                        getval("value", conId),
                        getval("cost", conId),
                        getval("unrealized", conId),
                        getval("p&l", conId),
                    )
                elif isinstance(pos.contract, Option):
                    table.add_row(
                        "",
                        pos.contract.right,
                        getval("qty", conId),
                        getval("mktprice", conId),
                        getval("avgprice", conId),
                        getval("value", conId),
                        getval("cost", conId),
                        getval("unrealized", conId),
                        getval("p&l", conId),
                        getval("strike", conId),
                        getval("exp", conId),
                        getval("dte", conId),
                        getval("itm?", conId),
                    )

        first = True
        for symbol, position in portfolio_positions.items():
            if not first:
                table.add_section()
            first = False
            add_symbol_positions(symbol, position)

        if untracked_positions:
            table.add_section()
            table.add_row("Not tracked")
            table.add_section()
            first_untracked = True
            for symbol, position in untracked_positions.items():
                if not first_untracked:
                    table.add_section()
                first_untracked = False
                add_symbol_positions(symbol, position)

        log.print(table)

        return (account_summary, portfolio_positions)

    async def manage(self) -> None:
        had_error = False
        try:
            if self.data_store:
                self.data_store.record_event("run_start", {"dry_run": self.dry_run})
            self.initialize_account()
            (account_summary, portfolio_positions) = await self.summarize_account()

            options_enabled = self.options_trading_enabled()
            enabled_stages = set(self.run_stage_order)
            stage_index = {
                stage_id: idx for idx, stage_id in enumerate(self.run_stage_order)
            }
            close_stage_handled = False
            options_disabled_notice_logged = False
            positions_might_be_stale = False

            write_stage_ids = {"options_write_puts", "options_write_calls"}
            management_stage_ids = {"options_roll_positions", "options_close_positions"}
            post_stage_ids = {"post_vix_call_hedge", "post_cash_management"}
            option_stage_ids = write_stage_ids | management_stage_ids
            refresh_before_stage_ids = management_stage_ids | post_stage_ids
            pre_management_trade_stage_ids = {
                "options_write_puts",
                "options_write_calls",
                "equity_regime_rebalance",
                "equity_buy_rebalance",
                "equity_sell_rebalance",
            }

            for stage_id in self.run_stage_order:
                if stage_id in option_stage_ids and not options_enabled:
                    if not options_disabled_notice_logged:
                        log.notice(
                            "Regime rebalancing shares-only enabled; skipping option writes and rolls."
                        )
                        options_disabled_notice_logged = True
                    continue

                if stage_id in refresh_before_stage_ids and positions_might_be_stale:
                    portfolio_positions = await self.get_portfolio_positions()
                    positions_might_be_stale = False

                if stage_id in write_stage_ids:
                    await run_option_write_stages(
                        self._options_strategy_deps({stage_id}),
                        account_summary,
                        portfolio_positions,
                        options_enabled,
                    )
                elif stage_id == "options_roll_positions":
                    if (
                        "options_close_positions" in enabled_stages
                        and stage_index[stage_id]
                        < stage_index["options_close_positions"]
                    ):
                        await run_option_management_stages(
                            self._options_strategy_deps(
                                {"options_roll_positions", "options_close_positions"}
                            ),
                            account_summary,
                            portfolio_positions,
                            options_enabled,
                        )
                        close_stage_handled = True
                    else:
                        await run_option_management_stages(
                            self._options_strategy_deps({"options_roll_positions"}),
                            account_summary,
                            portfolio_positions,
                            options_enabled,
                        )
                elif stage_id == "options_close_positions":
                    if close_stage_handled:
                        continue
                    await run_option_management_stages(
                        self._options_strategy_deps({"options_close_positions"}),
                        account_summary,
                        portfolio_positions,
                        options_enabled,
                    )
                elif stage_id in {
                    "equity_regime_rebalance",
                    "equity_buy_rebalance",
                    "equity_sell_rebalance",
                }:
                    await run_equity_rebalance_stages(
                        self._equity_strategy_deps({stage_id}),
                        account_summary,
                        portfolio_positions,
                    )
                elif stage_id in post_stage_ids:
                    await run_post_stages(
                        self._post_strategy_deps({stage_id}),
                        account_summary,
                        portfolio_positions,
                    )

                if stage_id in pre_management_trade_stage_ids:
                    positions_might_be_stale = True

            if self.dry_run:
                log.warning("Dry run enabled, no trades will be executed.")

                self.orders.print_summary()
            else:
                self.submit_orders()

                try:
                    await self.ibkr.wait_for_submitting_orders(self.trades.records())
                except RuntimeError as exc:
                    # DAY orders can remain working at the broker after submission.
                    # Keep running and let later status checks/logs report open orders.
                    log.warning(f"Order submission wait timed out: {exc}")

                await self.adjust_prices()

                try:
                    await self.ibkr.wait_for_submitting_orders(self.trades.records())
                except RuntimeError as exc:
                    log.warning(f"Post-adjust order submission wait timed out: {exc}")
                working_statuses = {"PendingSubmit", "PreSubmitted", "Submitted"}
                incomplete_trades = [
                    trade
                    for trade in self.trades.records()
                    if trade and not trade.isDone()
                ]
                still_working = [
                    trade
                    for trade in incomplete_trades
                    if getattr(trade.orderStatus, "status", "") in working_statuses
                ]
                unexpected_state = [
                    trade for trade in incomplete_trades if trade not in still_working
                ]
                open_orders = ", ".join(
                    f"{trade.contract.symbol} (OrderId: {trade.order.orderId}, status={getattr(trade.orderStatus, 'status', 'UNKNOWN')})"
                    for trade in still_working
                )
                if open_orders:
                    log.info(
                        "Run completed with working submitted orders still open at broker: "
                        f"{open_orders}"
                    )
                if unexpected_state:
                    unexpected_orders = ", ".join(
                        f"{trade.contract.symbol} (OrderId: {trade.order.orderId}, status={getattr(trade.orderStatus, 'status', 'UNKNOWN')})"
                        for trade in unexpected_state
                    )
                    log.warning(
                        "Run completed with non-working incomplete orders at broker: "
                        f"{unexpected_orders}"
                    )

            log.info("ThetaGang is done, shutting down! Cya next time. :sparkles:")
        except:
            had_error = True
            log.error("ThetaGang terminated with error...")
            raise

        finally:
            # Shut it down
            if self.data_store:
                self.data_store.record_event("run_end", {"success": not had_error})
            self.completion_future.set_result(True)

    async def check_puts(
        self, portfolio_positions: Dict[str, List[PortfolioItem]]
    ) -> Tuple[List[Any], List[Any], Group]:
        return await self.options_engine.check_puts(portfolio_positions)

    async def check_calls(
        self, portfolio_positions: Dict[str, List[PortfolioItem]]
    ) -> Tuple[List[Any], List[Any], Group]:
        return await self.options_engine.check_calls(portfolio_positions)

    async def get_maximum_new_contracts_for(
        self,
        symbol: str,
        primary_exchange: str,
        account_summary: Dict[str, AccountValue],
    ) -> int:
        total_buying_power = self.get_buying_power(account_summary)
        max_buying_power = (
            self.config.strategies.wheel.defaults.target.maximum_new_contracts_percent
            * total_buying_power
        )
        ticker = await self.ibkr.get_ticker_for_stock(
            symbol,
            primary_exchange,
        )
        price = midpoint_or_market_price(ticker)
        return max([1, round((max_buying_power / price) // 100)])

    async def check_for_uncovered_positions(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Tuple[Table, List[Tuple[str, str, int, int]]]:
        self._sync_options_engine_state()
        return await self.options_engine.check_for_uncovered_positions(
            account_summary, portfolio_positions
        )

    async def write_calls(self, calls: List[Any]) -> None:
        self._sync_options_engine_state()
        await self.options_engine.write_calls(calls)

    async def write_puts(
        self, puts: List[Tuple[str, str, int, Optional[float]]]
    ) -> None:
        self._sync_options_engine_state()
        await self.options_engine.write_puts(puts)

    def get_primary_exchange(self, symbol: str) -> str:
        return self.config.portfolio.symbols[symbol].primary_exchange

    def _buying_power_with_margin(
        self, account_summary: Dict[str, AccountValue], margin_usage: float
    ) -> int:
        return math.floor(float(account_summary["NetLiquidation"].value) * margin_usage)

    def _resolve_margin_usage(self, resolver_name: str) -> float:
        fallback_raw = self.config.runtime.account.margin_usage
        fallback = (
            float(fallback_raw)
            if isinstance(fallback_raw, (int, float))
            and not isinstance(fallback_raw, bool)
            else 1.0
        )
        resolver = getattr(self.config, resolver_name, None)
        if not callable(resolver):
            return fallback
        try:
            resolved = resolver()
        except Exception:
            return fallback
        if isinstance(resolved, bool) or not isinstance(resolved, (int, float)):
            return fallback
        return float(resolved)

    def get_wheel_buying_power(self, account_summary: Dict[str, AccountValue]) -> int:
        margin_usage = self._resolve_margin_usage("wheel_margin_usage")
        return self._buying_power_with_margin(account_summary, margin_usage)

    def get_regime_buying_power(self, account_summary: Dict[str, AccountValue]) -> int:
        margin_usage = self._resolve_margin_usage("regime_margin_usage")
        return self._buying_power_with_margin(account_summary, margin_usage)

    def get_buying_power(self, account_summary: Dict[str, AccountValue]) -> int:
        return self.get_wheel_buying_power(account_summary)

    def midpoint_or_market_price(self, ticker: Ticker) -> float:
        return float(midpoint_or_market_price(ticker))

    def format_weight_info(
        self,
        symbol: str,
        position_values: Dict[str, float],
        weight_base_value: float,
    ) -> Tuple[str, str]:
        symbol_configs = resolve_symbol_configs(
            self.config, context="portfolio weight formatting"
        )
        return self.options_engine.format_weight_info(
            symbol, position_values, weight_base_value, symbol_configs
        )

    async def check_if_can_write_puts(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Tuple[Table, Table, List[Tuple[str, str, int, Optional[float]]]]:
        self._sync_options_engine_state()
        return await self.options_engine.check_if_can_write_puts(
            account_summary, portfolio_positions
        )

    async def _get_regime_proxy_series(
        self,
        symbols: List[str],
        lookback_days: int,
        cooldown_days: int,
        weights_override: Optional[Dict[str, float]] = None,
    ) -> Tuple[List[date], List[float], Dict[str, List[float]]]:
        return await self.regime_engine._get_regime_proxy_series(
            symbols, lookback_days, cooldown_days, weights_override
        )

    async def _get_regime_aligned_closes(
        self,
        symbols: List[str],
        lookback_days: int,
        cooldown_days: int,
    ) -> Tuple[List[date], Dict[str, List[float]]]:
        return await self.regime_engine._get_regime_aligned_closes(
            symbols, lookback_days, cooldown_days
        )

    async def _get_last_regime_rebalance_time(
        self, symbols: List[str]
    ) -> Optional[datetime]:
        return await self.regime_engine._get_last_regime_rebalance_time(symbols)

    def _cooldown_elapsed(self, last_rebalance: datetime, cooldown_days: int) -> bool:
        return self.regime_engine._cooldown_elapsed(last_rebalance, cooldown_days)

    async def check_regime_rebalance_positions(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Tuple[Table, List[Tuple[str, str, int]]]:
        return await self.regime_engine.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )

    async def execute_regime_rebalance_orders(
        self, orders: List[Tuple[str, str, int]]
    ) -> None:
        await self.equity_engine.execute_regime_rebalance_orders(orders)

    async def check_buy_only_positions(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Tuple[Table, List[Tuple[str, str, int]]]:
        return await self.equity_engine.check_buy_only_positions(
            account_summary, portfolio_positions
        )

    async def execute_buy_orders(self, buy_orders: List[Tuple[str, str, int]]) -> None:
        await self.equity_engine.execute_buy_orders(buy_orders)

    async def check_sell_only_positions(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Tuple[Table, List[Tuple[str, str, int]]]:
        return await self.equity_engine.check_sell_only_positions(
            account_summary, portfolio_positions
        )

    async def execute_sell_orders(
        self, sell_orders: List[Tuple[str, str, int]]
    ) -> None:
        await self.equity_engine.execute_sell_orders(sell_orders)

    async def close_puts(self, puts: List[PortfolioItem]) -> None:
        await self.options_engine.close_puts(puts)

    async def roll_puts(
        self,
        puts: List[PortfolioItem],
        account_summary: Dict[str, AccountValue],
    ) -> List[PortfolioItem]:
        return await self.options_engine.roll_puts(puts, account_summary)

    async def close_calls(self, calls: List[PortfolioItem]) -> None:
        await self.options_engine.close_calls(calls)

    async def roll_calls(
        self,
        calls: List[PortfolioItem],
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> List[PortfolioItem]:
        return await self.options_engine.roll_calls(
            calls, account_summary, portfolio_positions
        )

    async def close_positions(self, right: str, positions: List[PortfolioItem]) -> None:
        await self.options_engine.close_positions(right, positions)

    async def roll_positions(
        self,
        positions: List[PortfolioItem],
        right: str,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Optional[Dict[str, List[PortfolioItem]]] = None,
    ) -> List[PortfolioItem]:
        return await self.options_engine.roll_positions(
            positions, right, account_summary, portfolio_positions
        )

    async def do_vix_hedging(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> None:
        await self.post_engine.do_vix_hedging(account_summary, portfolio_positions)

    def calc_pending_cash_balance(self) -> float:
        return self.post_engine.calc_pending_cash_balance()

    async def do_cashman(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> None:
        await self.post_engine.do_cashman(account_summary, portfolio_positions)

    def submit_orders(self) -> None:
        for contract, order, intent_id in self.orders.records():
            self.trades.submit_order(contract, order, intent_id=intent_id)
        self.trades.print_summary()

    async def adjust_prices(self) -> None:
        if (
            all(
                [
                    not self.config.portfolio.symbols[symbol].adjust_price_after_delay
                    for symbol in self.config.portfolio.symbols
                ]
            )
            or self.trades.is_empty()
        ):
            log.warning("Skipping order price adjustments...")
            return

        delay = random.randrange(
            self.config.runtime.orders.price_update_delay[0],
            self.config.runtime.orders.price_update_delay[1],
        )

        await self.ibkr.wait_for_orders_complete(self.trades.records(), delay)

        unfilled = [
            (idx, trade)
            for idx, trade in enumerate(self.trades.records())
            if trade
            and trade.contract.symbol in self.config.portfolio.symbols
            and self.config.portfolio.symbols[
                trade.contract.symbol
            ].adjust_price_after_delay
            and not trade.isDone()
        ]

        for idx, trade in unfilled:
            try:
                # Bound midpoint price requests so repricing never blocks run termination.
                ticker = await asyncio.wait_for(
                    self.ibkr.get_ticker_for_contract(
                        trade.contract,
                        required_fields=[TickerField.MIDPOINT],
                        optional_fields=[TickerField.MARKET_PRICE],
                    ),
                    timeout=self.config.runtime.ib_async.api_response_wait_time,
                )

                (contract, order) = (trade.contract, trade.order)
                updated_price = np.sign(float(order.lmtPrice or 0)) * max(
                    [
                        (
                            self.config.runtime.orders.minimum_credit
                            if order.action == "BUY"
                            and float(order.lmtPrice or 0) <= 0.0
                            else 0.0
                        ),
                        math.fabs(
                            round(
                                (float(order.lmtPrice or 0) + ticker.midpoint()) / 2.0,
                                2,
                            )
                        ),
                    ]
                )

                if trade.contract.symbol == "VIX":
                    # Round VIX prices according to contract specifications
                    updated_price = self.order_ops.round_vix_price(updated_price)

                # We only want to tighten spreads, not widen them. If the
                # resulting price change would increase the spread, we'll
                # skip it.
                if would_increase_spread(order, updated_price):
                    log.warning(
                        f"Skipping order for {contract.symbol}"
                        f" with old lmtPrice={dfmt(float(order.lmtPrice or 0))} updated lmtPrice={dfmt(updated_price)}, because updated price would increase spread"
                    )
                    return

                # Check if the updated price is actually any different
                # before proceeding, and make sure the signs match so we
                # don't switch a credit to a debit or vice versa.
                if float(order.lmtPrice or 0) != updated_price and np.sign(
                    float(order.lmtPrice or 0)
                ) == np.sign(updated_price):
                    log.info(
                        f"{contract.symbol}: Resubmitting {order.action} {contract.secType} order with old lmtPrice={dfmt(float(order.lmtPrice or 0))} updated lmtPrice={dfmt(updated_price)}"
                    )

                    # For some reason, we need to create a new order object
                    # and populate the fields rather than modifying the
                    # existing order in-place (janky).
                    order = LimitOrder(
                        order.action,
                        order.totalQuantity,
                        float(updated_price),
                        orderId=order.orderId,
                        algoStrategy=order.algoStrategy,
                        algoParams=order.algoParams,
                    )

                    # resubmit the order and it will be placed back to the
                    # original position in the queue
                    self.trades.submit_order(contract, order, idx)

                    log.info(f"{contract.symbol}: Order updated, order={order}")
            except (
                asyncio.TimeoutError,
                RuntimeError,
                RequiredFieldValidationError,
            ) as exc:
                log.warning(
                    f"Couldn't generate midpoint price for {trade.contract}, skipping repricing"
                )
                if self.data_store:
                    self.data_store.record_event(
                        "order_price_adjustment_skipped",
                        {
                            "symbol": getattr(trade.contract, "symbol", ""),
                            "secType": getattr(trade.contract, "secType", ""),
                            "reason": type(exc).__name__,
                        },
                    )
                continue

    async def get_write_threshold(
        self, ticker: Ticker, right: str
    ) -> tuple[float, float]:
        assert ticker.contract is not None
        close_price = self.get_close_price(ticker)
        absolute_daily_change = math.fabs(ticker.marketPrice() - close_price)

        threshold_sigma = self.config.get_write_threshold_sigma(
            ticker.contract.symbol,
            right,
        )
        if threshold_sigma:
            hist_prices = await self.ibkr.request_historical_data(
                ticker.contract,
                self.config.strategies.wheel.defaults.constants.daily_stddev_window,
            )
            log_prices = np.log(np.array([p.close for p in hist_prices]))
            stddev = np.std(np.diff(log_prices), ddof=1)

            return (
                close_price * (np.exp(stddev) - 1).astype(float) * threshold_sigma,
                absolute_daily_change,
            )

        threshold_perc = self.config.get_write_threshold_perc(
            ticker.contract.symbol,
            right,
        )
        return (threshold_perc * close_price, absolute_daily_change)
