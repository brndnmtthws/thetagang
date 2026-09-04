import asyncio
import copy
import logging
import math
import random
from asyncio import Future
from collections import Counter
from collections.abc import Coroutine
from datetime import datetime
from typing import Any, cast

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
from rich.panel import Panel
from rich.table import Table

from thetagang import log
from thetagang.accounting import (
    AccountMetric,
    AccountSummary,
    BrokerAccountSnapshot,
    CapitalBaseKind,
    PortfolioAccounting,
)
from thetagang.config import (
    CANONICAL_STAGE_ORDER,
    DEFAULT_RUN_STRATEGIES,
    Config,
    RunConfig,
    enabled_stage_ids_from_run,
    stage_enabled_map_from_run,
)
from thetagang.db import DataStore
from thetagang.external_decisions import ExternalDecisionProviders
from thetagang.fmt import dfmt, ffmt, ifmt, pfmt
from thetagang.ibkr import IBKR
from thetagang.order_execution import OrderExecutionManager
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
from thetagang.strategies.regime_engine import (
    ABSOLUTE_TREND_STATE_EVENT,
    RegimeRebalanceEngine,
)
from thetagang.strategies.runtime_services import (
    EquityRuntimeServiceAdapter,
    OptionsRuntimeServiceAdapter,
)
from thetagang.strategies.tail_hedge_state import (
    TAIL_HEDGE_CLOSE_ORDER_REF,
    TAIL_HEDGE_ENTRY_ORDER_REF,
    TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX,
    TailHedgeStateStore,
    is_tail_order_ref,
    is_tail_reduction_ref,
)
from thetagang.target_weight_policy import TARGET_WEIGHT_POLICY_STATE_EVENT
from thetagang.trades import Trades
from thetagang.trading_operations import (
    OptionChainScanner,
    OrderOperations,
)
from thetagang.util import (
    account_summary_to_dict,
    midpoint_or_market_price,
    portfolio_positions_to_dict,
    position_pnl,
    working_stock_order_symbols,
)

from .options import option_dte

# Turn off some of the more annoying logging output from ib_async
logging.getLogger("ib_async.ib").setLevel(logging.ERROR)
logging.getLogger("ib_async.wrapper").setLevel(logging.CRITICAL)

TAIL_HARVEST_FILL_TIMEOUT_SECONDS = 5 * 60
REGIME_PLANNING_STATE_EVENTS = {
    ABSOLUTE_TREND_STATE_EVENT,
    TARGET_WEIGHT_POLICY_STATE_EVENT,
    "regime_rebalance_state",
    "volatility_weight_state",
}


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
        data_store: DataStore | None = None,
        run_stage_flags: dict[str, bool] | None = None,
        run_stage_order: list[str] | None = None,
    ) -> None:
        self.account_number = config.runtime.account.number
        self.config = config
        self.data_store = data_store
        external_decision_config = getattr(
            self.config.runtime, "external_decisions", None
        )
        self.external_decisions = ExternalDecisionProviders(
            getattr(external_decision_config, "providers", {})
        )
        self.ibkr = IBKR(
            ib,
            config.runtime.ib_async.api_response_wait_time,
            config.runtime.orders.exchange,
            data_store=data_store,
            dry_run=dry_run,
        )
        self.completion_future = completion_future
        self.has_excess_calls: set[str] = set()
        self.has_excess_puts: set[str] = set()
        self.orders: Orders = Orders()
        self.trades: Trades = Trades(self.ibkr, data_store=data_store)
        self.execution_manager = OrderExecutionManager(
            config=self.config,
            ibkr=self.ibkr,
            data_store=self.data_store,
        )
        self.target_quantities: dict[str, int] = {}
        self.qualified_contracts: dict[int, Contract] = {}
        self.dry_run = dry_run
        self.last_untracked_positions: dict[str, list[PortfolioItem]] = {}
        self._reserved_cash_for_post_management = 0.0
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
            data_store=self.data_store,
        )
        self.regime_engine = RegimeRebalanceEngine(
            config=self.config,
            ibkr=self.ibkr,
            order_ops=self.order_ops,
            data_store=self.data_store,
            external_decisions=self.external_decisions,
            dry_run=self.dry_run,
            get_primary_exchange=self.get_primary_exchange,
            now_provider=lambda: datetime.now(),  # noqa: DTZ005
            tail_hedge_stage_enabled=lambda: self.stage_enabled("post_tail_hedge"),
            set_reserved_cash_for_post_management=(
                self.set_reserved_cash_for_post_management
            ),
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
            data_store=self.data_store,
            get_reserved_cash_for_post_management=(
                self.get_reserved_cash_for_post_management
            ),
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

    def set_reserved_cash_for_post_management(self, amount: float) -> None:
        self._reserved_cash_for_post_management = max(0.0, amount)

    def get_reserved_cash_for_post_management(self) -> float:
        return self._reserved_cash_for_post_management

    async def put_is_itm(self, contract: Contract) -> bool:
        return await self.options_engine.put_is_itm(contract)

    async def call_is_itm(self, contract: Contract) -> bool:
        return await self.options_engine.call_is_itm(contract)

    def get_symbols(self) -> list[str]:
        return list(self.config.portfolio.symbols.keys())

    def partition_positions(
        self, portfolio_positions: list[PortfolioItem]
    ) -> tuple[list[PortfolioItem], list[PortfolioItem]]:
        symbols = self.get_symbols()
        tracked_positions: list[PortfolioItem] = []
        untracked_positions: list[PortfolioItem] = []
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

    @staticmethod
    def combine_position_maps(
        *position_maps: dict[str, list[PortfolioItem]],
    ) -> dict[str, list[PortfolioItem]]:
        combined: dict[str, list[PortfolioItem]] = {}
        for position_map in position_maps:
            for symbol, positions in position_map.items():
                combined.setdefault(symbol, []).extend(positions)
        return combined

    def _tail_hedge_owned_con_ids_for_snapshot(self) -> set[int]:
        if self.data_store is None:
            return set()
        ownership_required = bool(
            self.stage_enabled("post_tail_hedge")
            and self.config.strategies.tail_hedge.enabled
        )
        try:
            return (
                TailHedgeStateStore(
                    self.data_store,
                    self.account_number,
                )
                .load(raise_on_error=ownership_required)
                .owned_con_ids
            )
        except RuntimeError:
            if ownership_required:
                raise
            return set()

    def get_portfolio_positions(self) -> dict[str, list[PortfolioItem]]:
        """Materialize the account's current ib_async portfolio cache."""
        portfolio_positions = self.ibkr.portfolio(account=self.account_number)
        filtered_positions, untracked_positions = self.partition_positions(
            portfolio_positions
        )
        self.last_untracked_positions = portfolio_positions_to_dict(untracked_positions)
        return portfolio_positions_to_dict(filtered_positions)

    async def load_initial_portfolio_positions(
        self,
    ) -> dict[str, list[PortfolioItem]]:
        """Validate the synchronized startup caches before trading."""
        attempts = 3
        symbols = set(self.get_symbols())
        tail_hedge_con_ids = self._tail_hedge_owned_con_ids_for_snapshot()

        for attempt in range(1, attempts + 1):
            portfolio_by_symbol = self.get_portfolio_positions()
            all_portfolio_positions = self.combine_position_maps(
                portfolio_by_symbol,
                self.last_untracked_positions,
            )
            portfolio_conids = {
                item.contract.conId
                for positions in all_portfolio_positions.values()
                for item in positions
            }
            protected_positions = [
                pos
                for pos in self.ibkr.positions(self.account_number)
                if pos.account == self.account_number
                and (
                    pos.contract.symbol in symbols
                    or pos.contract.symbol == "VIX"
                    or pos.contract.symbol
                    == self.config.strategies.cash_management.cash_fund
                    or pos.contract.conId in tail_hedge_con_ids
                )
                and pos.position != 0
            ]
            missing_positions = [
                pos
                for pos in protected_positions
                if pos.contract.conId not in portfolio_conids
            ]

            if not missing_positions:
                return portfolio_by_symbol

            missing_symbols = ", ".join(
                sorted({pos.contract.symbol for pos in missing_positions})
            )
            log.warning(
                f"Attempt {attempt}/{attempts}: Portfolio snapshot is missing "
                f"{len(missing_positions)} of {len(protected_positions)} tracked "
                f"or tail-hedge-owned positions (symbols: {missing_symbols}). "
                "Waiting briefly before retrying..."
            )
            await asyncio.sleep(1)

        raise RuntimeError(
            "Failed to load IBKR portfolio positions after multiple attempts. "
            "Aborting run to avoid trading on incomplete data."
        )

    def _is_startup_cancel_candidate(self, trade: Any) -> bool:
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        symbol = getattr(contract, "symbol", None)
        return bool(
            order is not None
            and contract is not None
            and not trade.isDone()
            and getattr(order, "account", "") == self.account_number
            and (
                symbol in self.get_symbols()
                or (self.config.strategies.vix_call_hedge.enabled and symbol == "VIX")
                or (
                    self.config.strategies.cash_management.enabled
                    and symbol == self.config.strategies.cash_management.cash_fund
                )
            )
        )

    def initialize_account(self) -> None:
        self.ibkr.set_market_data_type(self.config.runtime.account.market_data_type)

        open_trades = self.ibkr.open_trades()
        for trade in open_trades:
            order = getattr(trade, "order", None)
            if (
                order is None
                or trade.isDone()
                or getattr(order, "account", None) != self.account_number
                or getattr(order, "orderRef", None) != TAIL_HEDGE_ENTRY_ORDER_REF
            ):
                continue
            if self.dry_run:
                log.warning(
                    f"{trade.contract.symbol}: Dry run, would cancel stale tail entry "
                    f"{order}"
                )
            else:
                log.warning(
                    f"{trade.contract.symbol}: Canceling stale tail entry {order}"
                )
                self.ibkr.cancel_order(order)

        if not self.config.runtime.account.cancel_orders:
            return

        for trade in open_trades:
            if not self._is_startup_cancel_candidate(trade):
                continue
            order_ref = getattr(trade.order, "orderRef", None)
            if order_ref == TAIL_HEDGE_ENTRY_ORDER_REF:
                continue
            if is_tail_order_ref(order_ref):
                log.info(
                    f"{trade.contract.symbol}: Preserving tail order {trade.order}"
                )
                continue
            if self.dry_run:
                log.warning(
                    f"{trade.contract.symbol}: Dry run, would cancel order "
                    f"{trade.order}"
                )
                continue
            log.warning(f"{trade.contract.symbol}: Canceling order {trade.order}")
            self.ibkr.cancel_order(trade.order)

    async def summarize_account(
        self,
    ) -> tuple[
        AccountSummary,
        dict[str, list[PortfolioItem]],
    ]:
        account_summary = await self.ibkr.account_summary(self.account_number)
        account_summary = account_summary_to_dict(account_summary)

        if AccountMetric.NET_LIQUIDATION.value not in account_summary:
            raise RuntimeError(
                f"Account number {self.config.runtime.account.number} appears invalid (no account data returned)"
            )

        account = BrokerAccountSnapshot(account_summary)

        table = Table(title="Account summary")
        table.add_column("Item")
        table.add_column("Value", justify="right")
        table.add_row("Net liquidation", dfmt(account.net_liquidation, 0))
        table.add_row(
            "Excess liquidity", dfmt(account.value(AccountMetric.EXCESS_LIQUIDITY), 0)
        )
        table.add_row(
            "Initial margin", dfmt(account.value(AccountMetric.INITIAL_MARGIN), 0)
        )
        table.add_row(
            "Maintenance margin",
            dfmt(account.value(AccountMetric.MAINTENANCE_MARGIN), 0),
        )
        table.add_row(
            "Buying power", dfmt(account.value(AccountMetric.BROKER_BUYING_POWER), 0)
        )
        table.add_row("Total cash", dfmt(account.total_cash, 0))
        table.add_row("Cushion", pfmt(account.value(AccountMetric.CUSHION), 0))
        table.add_section()
        table.add_row(
            "Target buying power usage", dfmt(self.get_buying_power(account_summary), 0)
        )
        log.print(Panel(table))

        portfolio_positions = await self.load_initial_portfolio_positions()
        untracked_positions = self.last_untracked_positions
        if self.data_store:
            self.data_store.record_account_snapshot(account_summary)
            self.data_store.record_positions_snapshot(
                self.combine_position_maps(
                    portfolio_positions,
                    untracked_positions,
                )
            )

        position_values: dict[int, dict[str, str]] = {}

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

        tasks: list[Coroutine[Any, Any, None]] = []
        for positions in portfolio_positions.values():
            for position in positions:
                tasks.append(load_position_task(position))
        for positions in untracked_positions.values():
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

        def add_symbol_positions(symbol: str, positions: list[PortfolioItem]) -> None:
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

    @staticmethod
    def _is_tail_harvest_record(
        record: tuple[Contract, LimitOrder, int | None],
    ) -> bool:
        order_ref = getattr(record[1], "orderRef", None)
        return isinstance(order_ref, str) and order_ref.startswith(
            f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:"
        )

    async def _refresh_account_state(
        self,
    ) -> tuple[AccountSummary, dict[str, list[PortfolioItem]]]:
        await asyncio.wait_for(
            self.ibkr.refresh_account(self.account_number),
            timeout=self.config.runtime.ib_async.api_response_wait_time,
        )
        account_summary = account_summary_to_dict(
            await self.ibkr.account_summary(self.account_number)
        )
        for tag in (
            AccountMetric.NET_LIQUIDATION.value,
            AccountMetric.TOTAL_CASH.value,
        ):
            value = self.ibkr.cached_account_value(self.account_number, tag)
            account_summary[tag] = AccountValue(
                self.account_number,
                tag,
                str(value),
                "BASE",
                "",
            )
        return account_summary, self.get_portfolio_positions()

    def _discard_current_regime_planning_state(self) -> None:
        if self.data_store:
            self.data_store.discard_current_run_events(REGIME_PLANNING_STATE_EVENTS)

    async def _plan_regime_rebalance(
        self,
        account_summary: AccountSummary,
        portfolio_positions: dict[str, list[PortfolioItem]],
        *,
        exclude_current_run_state: bool = False,
    ) -> list[tuple[str, str, int]]:
        table, orders = await self.equity_engine.check_regime_rebalance_positions(
            account_summary,
            self.combine_position_maps(
                portfolio_positions,
                self.last_untracked_positions,
            ),
            exclude_current_run_state=exclude_current_run_state,
        )
        if self.config.strategies.regime_rebalance.enabled:
            log.print(table)
        return orders

    async def _run_regime_rebalance_stage(
        self,
        account_summary: AccountSummary,
        portfolio_positions: dict[str, list[PortfolioItem]],
    ) -> tuple[AccountSummary, dict[str, list[PortfolioItem]]]:
        records_before = len(self.orders.records())
        regime_orders = await self._plan_regime_rebalance(
            account_summary,
            portfolio_positions,
        )
        new_records = self.orders.records()[records_before:]
        harvest_records = [
            record for record in new_records if self._is_tail_harvest_record(record)
        ]
        if self.dry_run or not harvest_records:
            if regime_orders:
                await self.equity_engine.execute_regime_rebalance_orders(regime_orders)
            return account_summary, portfolio_positions

        self._discard_current_regime_planning_state()
        self.orders.remove_records(harvest_records)
        if not await self._execute_tail_harvest_phase(harvest_records):
            raise RuntimeError("Tail-harvest execution did not fully fill")

        account_summary, portfolio_positions = await self._refresh_account_state()
        records_before = len(self.orders.records())
        regime_orders = await self._plan_regime_rebalance(
            account_summary,
            portfolio_positions,
            exclude_current_run_state=True,
        )
        extra_harvests = [
            record
            for record in self.orders.records()[records_before:]
            if self._is_tail_harvest_record(record)
        ]
        if extra_harvests:
            self.orders.remove_records(extra_harvests)
            for contract, _order, _intent_id in extra_harvests:
                con_id = getattr(contract, "conId", 0)
                if type(con_id) is int and con_id > 0:
                    self._update_tail_recovery_submission(con_id, None)
            self._discard_current_regime_planning_state()
            log.error(
                "A second tail harvest was queued during post-fill replanning; "
                "aborting remaining stages safely."
            )
            raise RuntimeError("Tail-harvest replanning did not serialize")

        if regime_orders:
            records_before = len(self.orders.records())
            prepared = await self.equity_engine.execute_regime_rebalance_orders(
                regime_orders
            )
            if prepared != len(regime_orders):
                added_records = self.orders.records()[records_before:]
                self.orders.remove_records(added_records)
                self._discard_current_regime_planning_state()
                log.error(
                    "Post-harvest regime orders were not prepared completely; "
                    "aborting before final submission."
                )
                raise RuntimeError("Post-harvest rebalance preparation was incomplete")
        return account_summary, portfolio_positions

    async def manage(self) -> None:
        had_error = False
        try:
            self.regime_engine.begin_run()
            self.set_reserved_cash_for_post_management(0.0)
            if self.data_store:
                self.data_store.record_event("run_start", {"dry_run": self.dry_run})
            self.initialize_account()
            (account_summary, portfolio_positions) = await self.summarize_account()

            enabled_stages = set(self.run_stage_order)
            stage_index = {
                stage_id: idx for idx, stage_id in enumerate(self.run_stage_order)
            }
            close_stage_handled = False
            positions_might_be_stale = False

            write_stage_ids = {"options_write_puts", "options_write_calls"}
            management_stage_ids = {"options_roll_positions", "options_close_positions"}
            post_stage_ids = {
                "post_vix_call_hedge",
                "post_tail_hedge",
                "post_cash_management",
            }
            rematerialize_before_stage_ids = management_stage_ids | post_stage_ids
            pre_management_trade_stage_ids = {
                "options_write_puts",
                "options_write_calls",
                "equity_regime_rebalance",
                "equity_buy_rebalance",
                "equity_sell_rebalance",
            }

            for stage_id in self.run_stage_order:
                if (
                    stage_id in rematerialize_before_stage_ids
                    and positions_might_be_stale
                ):
                    portfolio_positions = self.get_portfolio_positions()
                    positions_might_be_stale = False

                if stage_id in write_stage_ids:
                    await run_option_write_stages(
                        self._options_strategy_deps({stage_id}),
                        account_summary,
                        portfolio_positions,
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
                        )
                        close_stage_handled = True
                    else:
                        await run_option_management_stages(
                            self._options_strategy_deps({"options_roll_positions"}),
                            account_summary,
                            portfolio_positions,
                        )
                elif stage_id == "options_close_positions":
                    if close_stage_handled:
                        continue
                    await run_option_management_stages(
                        self._options_strategy_deps({"options_close_positions"}),
                        account_summary,
                        portfolio_positions,
                    )
                elif stage_id == "equity_regime_rebalance":
                    (
                        account_summary,
                        portfolio_positions,
                    ) = await self._run_regime_rebalance_stage(
                        account_summary,
                        portfolio_positions,
                    )
                elif stage_id in {
                    "equity_buy_rebalance",
                    "equity_sell_rebalance",
                }:
                    await run_equity_rebalance_stages(
                        self._equity_strategy_deps({stage_id}),
                        account_summary,
                        portfolio_positions,
                    )
                elif stage_id in post_stage_ids:
                    post_positions = portfolio_positions
                    if stage_id == "post_tail_hedge":
                        post_positions = self.combine_position_maps(
                            portfolio_positions,
                            self.last_untracked_positions,
                        )
                    await run_post_stages(
                        self._post_strategy_deps({stage_id}),
                        account_summary,
                        post_positions,
                    )

                if stage_id in pre_management_trade_stage_ids:
                    positions_might_be_stale = True

            await self.execution_manager.prepare_orders(self.orders.records())

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

    async def get_maximum_new_contracts_for(
        self,
        symbol: str,
        primary_exchange: str,
        account_summary: AccountSummary,
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

    def get_primary_exchange(self, symbol: str) -> str:
        return self.config.portfolio.symbols[symbol].primary_exchange

    def get_wheel_buying_power(self, account_summary: AccountSummary) -> int:
        accounting = PortfolioAccounting.build(
            config=self.config,
            account_summary=account_summary,
        )
        return int(accounting.capital_base(CapitalBaseKind.WHEEL_BUYING_POWER).value)

    def get_buying_power(self, account_summary: AccountSummary) -> int:
        return self.get_wheel_buying_power(account_summary)

    def midpoint_or_market_price(self, ticker: Ticker) -> float:
        return float(midpoint_or_market_price(ticker))

    def _working_option_commitments(
        self, open_trades: list[Any]
    ) -> Counter[tuple[int, str]]:
        commitments: Counter[tuple[int, str]] = Counter()
        for trade in open_trades:
            contract = getattr(trade, "contract", None)
            order = getattr(trade, "order", None)
            if (
                contract is None
                or order is None
                or trade.isDone()
                or getattr(order, "account", None) != self.account_number
                or getattr(contract, "secType", None) != "OPT"
            ):
                continue
            con_id = getattr(contract, "conId", 0)
            action = str(getattr(order, "action", "")).upper()
            if type(con_id) is int and con_id > 0 and action in {"BUY", "SELL"}:
                commitments[(con_id, action)] += math.ceil(
                    max(0, float(order.totalQuantity))
                )
        return commitments

    def _live_position(self, con_id: int) -> float:
        try:
            position = sum(
                float(item.position)
                for item in self.ibkr.portfolio(account=self.account_number)
                if getattr(item, "account", None) == self.account_number
                and getattr(getattr(item, "contract", None), "conId", None) == con_id
            )
        except (TypeError, ValueError):
            return 0.0
        return position if math.isfinite(position) else 0.0

    def _has_live_stock(self, symbol: str) -> bool:
        try:
            shares = sum(
                float(item.position)
                for item in self.ibkr.portfolio(account=self.account_number)
                if getattr(item, "account", None) == self.account_number
                and isinstance(getattr(item, "contract", None), Stock)
                and item.contract.symbol == symbol
            )
        except (TypeError, ValueError):
            return False
        return math.isfinite(shares) and shares > 0

    def _has_live_contract(self, con_id: int) -> bool:
        """Fail closed when an entry contract's live occupancy is ambiguous."""
        try:
            for item in self.ibkr.portfolio(account=self.account_number):
                if (
                    getattr(item, "account", None) != self.account_number
                    or getattr(getattr(item, "contract", None), "conId", None) != con_id
                ):
                    continue
                quantity = float(item.position)
                if not math.isfinite(quantity) or not math.isclose(quantity, 0.0):
                    return True
        except (AttributeError, TypeError, ValueError):
            return True
        return False

    def _update_tail_recovery_submission(
        self,
        con_id: int,
        quantity: int | None,
        *,
        live_quantity: int | None = None,
    ) -> bool:
        if self.data_store is None:
            log.error("Cannot submit a tail reduction without durable state.")
            return False
        try:
            updated = TailHedgeStateStore(
                self.data_store,
                self.account_number,
            ).update_recovery_submission(
                con_id,
                quantity,
                live_quantity=live_quantity,
            )
        except RuntimeError as exc:
            log.error(f"Failed to update tail-reduction state: {exc}")
            return False
        if not updated:
            log.error(f"No pending tail-reduction state found for conId {con_id}.")
        return updated

    def _release_tail_entry_submission(self, con_id: int) -> bool:
        if self.data_store is None:
            log.error("Cannot release a skipped tail entry without durable state.")
            return False
        try:
            released = TailHedgeStateStore(
                self.data_store,
                self.account_number,
            ).release_entry_submission(con_id)
        except RuntimeError as exc:
            log.error(f"Failed to release skipped tail-entry state: {exc}")
            return False
        if not released:
            log.error(f"No pending tail-entry state found for conId {con_id}.")
        return released

    @staticmethod
    def _trade_fully_filled(trade: Any) -> bool:
        return OrderExecutionManager.trade_fully_filled(trade)

    async def _cancel_incomplete_trades(self, trades: list[Any]) -> None:
        canceled_trades = []
        for trade in trades:
            if self._trade_fully_filled(trade) or trade.isDone():
                continue
            log.warning(
                f"{trade.contract.symbol}: Canceling incomplete tail harvest "
                f"after bounded fill attempt."
            )
            self.ibkr.cancel_order(trade.order)
            canceled_trades.append(trade)
        if not canceled_trades:
            return
        still_working = await self.ibkr.wait_for_orders_complete(
            canceled_trades,
            max(
                1,
                min(60, self.config.runtime.ib_async.api_response_wait_time),
            ),
        )
        if still_working:
            log.error(
                "Tail-harvest cancellation was not confirmed before shutdown; "
                "no dependent orders will be submitted."
            )

    async def _execute_tail_harvest_phase(
        self,
        order_records: list[tuple[Contract, LimitOrder, int | None]],
        timeout: int = TAIL_HARVEST_FILL_TIMEOUT_SECONDS,
    ) -> bool:
        if not order_records:
            return True

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        trade_start = len(self.trades.records())
        self.submit_orders(order_records)
        trade_indices = list(range(trade_start, len(self.trades.records())))
        phase_trades = [self.trades.records()[idx] for idx in trade_indices]
        if len(phase_trades) != len(order_records):
            await self._cancel_incomplete_trades(phase_trades)
            log.error("Unable to submit every tail-harvest order; aborting safely.")
            return False

        remaining = deadline - loop.time()
        if remaining <= 0:
            await self._cancel_incomplete_trades(phase_trades)
            log.error("Tail-harvest fill deadline expired; aborting safely.")
            return False
        submit_timeout = max(1, math.ceil(min(60.0, remaining)))
        try:
            await self.ibkr.wait_for_submitting_orders(
                phase_trades,
                submit_timeout,
            )
        except RuntimeError as exc:
            await self._cancel_incomplete_trades(phase_trades)
            log.error(f"Tail-harvest submission did not settle: {exc}")
            return False

        delay = random.randrange(
            self.config.runtime.orders.price_update_delay[0],
            self.config.runtime.orders.price_update_delay[1],
        )
        remaining = deadline - loop.time()
        if remaining <= 0:
            await self._cancel_incomplete_trades(phase_trades)
            log.error("Tail-harvest fill deadline expired; aborting safely.")
            return False
        first_wait = max(1, math.ceil(min(float(delay), remaining)))
        incomplete = await self.ibkr.wait_for_orders_complete(
            phase_trades,
            first_wait,
        )

        if incomplete and loop.time() < deadline:
            incomplete_ids = {id(trade) for trade in incomplete}
            for idx in trade_indices:
                trade = self.trades.records()[idx]
                if id(trade) in incomplete_ids:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    await self._reprice_trade(idx, trade, timeout=remaining)

        phase_trades = [self.trades.records()[idx] for idx in trade_indices]
        incomplete = [
            trade for trade in phase_trades if not self._trade_fully_filled(trade)
        ]
        remaining = deadline - loop.time()
        if incomplete and remaining > 0:
            await self.ibkr.wait_for_orders_complete(
                incomplete,
                max(1, math.ceil(remaining)),
            )

        phase_trades = [self.trades.records()[idx] for idx in trade_indices]
        if all(self._trade_fully_filled(trade) for trade in phase_trades):
            log.notice("Tail-harvest orders filled; recalculating regime rebalance.")
            return True

        await self._cancel_incomplete_trades(phase_trades)
        log.error(
            f"Tail-harvest orders did not fully fill within {timeout} seconds; "
            "aborting remaining stages safely."
        )
        return False

    def submit_orders(
        self,
        order_records: list[tuple[Contract, LimitOrder, int | None]] | None = None,
    ) -> None:
        open_trades = self.ibkr.open_trades()
        commitments = self._working_option_commitments(open_trades)
        working_option_con_ids = {con_id for con_id, _action in commitments}
        working_tail_entry_con_ids = {
            int(trade.contract.conId)
            for trade in open_trades
            if not trade.isDone()
            and getattr(getattr(trade, "order", None), "account", None)
            == self.account_number
            and getattr(getattr(trade, "order", None), "orderRef", None)
            == TAIL_HEDGE_ENTRY_ORDER_REF
            and str(getattr(getattr(trade, "order", None), "action", "")).upper()
            == "BUY"
            and getattr(getattr(trade, "contract", None), "secType", None) == "OPT"
            and type(getattr(trade.contract, "conId", None)) is int
            and trade.contract.conId > 0
        }
        queued_records = self.orders.records()
        if order_records is None:
            order_records = queued_records
        working_stock_symbols = working_stock_order_symbols(
            open_trades,
            self.account_number,
        )
        working_stock_symbols |= {
            contract.symbol
            for contract, order, _intent_id in queued_records
            if isinstance(contract, Stock)
            and getattr(order, "account", None) == self.account_number
            and str(getattr(order, "action", "")).upper() in {"BUY", "SELL"}
        }
        submitted_tail_sells: set[int] = set()
        submitted_tail_entries: set[int] = set()
        for contract, order, intent_id in order_records:
            order_ref = getattr(order, "orderRef", None)
            submitted_order = order
            reduction_con_id: int | None = None

            if order_ref == TAIL_HEDGE_ENTRY_ORDER_REF:
                entry_con_id = getattr(contract, "conId", 0)
                entry_submitted_this_batch = (
                    type(entry_con_id) is int and entry_con_id in submitted_tail_entries
                )
                entry_owned_by_working_order = (
                    type(entry_con_id) is int
                    and entry_con_id in working_tail_entry_con_ids
                )
                entry_is_valid = not (
                    getattr(order, "account", None) != self.account_number
                    or str(getattr(order, "action", "")).upper() != "BUY"
                    or getattr(contract, "secType", None) != "OPT"
                    or type(entry_con_id) is not int
                    or entry_con_id <= 0
                )
                entry_is_occupied = entry_is_valid and (
                    entry_con_id in working_option_con_ids
                    or entry_submitted_this_batch
                    or self._has_live_contract(entry_con_id)
                )
                if (
                    not entry_is_valid
                    or not self._has_live_stock(contract.symbol)
                    or contract.symbol in working_stock_symbols
                    or entry_is_occupied
                ):
                    reason = (
                        "selected put contract is already occupied"
                        if entry_is_occupied
                        else "live underlying ownership is unstable"
                    )
                    log.warning(
                        f"{contract.symbol}: Skipping tail entry because {reason}."
                    )
                    # A same-batch duplicate or matching working entry shares
                    # this reservation with an order that may already be live.
                    # Every other skipped intent must release it so later
                    # reconciliation cannot adopt a foreign position.
                    if (
                        type(entry_con_id) is int
                        and entry_con_id > 0
                        and not entry_submitted_this_batch
                        and not entry_owned_by_working_order
                    ):
                        self._release_tail_entry_submission(entry_con_id)
                    continue

            if is_tail_reduction_ref(order_ref):
                con_id = getattr(contract, "conId", 0)
                action = str(getattr(order, "action", "")).upper()
                try:
                    requested = float(order.totalQuantity)
                except (TypeError, ValueError):
                    requested = 0.0
                valid_reduction = not (
                    getattr(order, "account", None) != self.account_number
                    or getattr(contract, "secType", None) != "OPT"
                    or type(con_id) is not int
                    or con_id <= 0
                    or action not in {"BUY", "SELL"}
                    or (order_ref != TAIL_HEDGE_CLOSE_ORDER_REF and action != "SELL")
                    or not math.isfinite(requested)
                    or requested <= 0
                    or not requested.is_integer()
                )
                live_capacity = 0
                if not valid_reduction:
                    capacity = 0
                else:
                    live_position = self._live_position(con_id)
                    closable = live_position if action == "SELL" else -live_position
                    live_capacity = max(0, math.floor(closable))
                    capacity = max(
                        0,
                        live_capacity - commitments[(con_id, action)],
                    )
                if action == "SELL" and con_id in submitted_tail_sells:
                    log.warning(
                        f"{contract.symbol}: Skipping duplicate tail reduction."
                    )
                    continue
                if capacity == 0:
                    if valid_reduction and action == "SELL" and live_capacity == 0:
                        self._update_tail_recovery_submission(con_id, None)
                    log.warning(
                        f"{contract.symbol}: Skipping tail close with no live capacity."
                    )
                    continue
                quantity = min(int(requested), capacity)
                if quantity != int(requested):
                    submitted_order = copy.deepcopy(order)
                    submitted_order.totalQuantity = quantity
                    log.warning(
                        f"{contract.symbol}: Resizing tail close to {quantity} contract(s)."
                    )
                if action == "SELL":
                    if not self._update_tail_recovery_submission(
                        con_id,
                        quantity,
                        live_quantity=math.floor(live_position),
                    ):
                        log.error(
                            f"{contract.symbol}: Skipping tail reduction without "
                            "synchronized durable state."
                        )
                        continue
                    reduction_con_id = con_id

            submitted = self.trades.submit_order(
                contract,
                submitted_order,
                intent_id=intent_id,
            )

            if not submitted and reduction_con_id is not None:
                self._update_tail_recovery_submission(reduction_con_id, None)

            if submitted and is_tail_reduction_ref(order_ref):
                key = (int(contract.conId), str(submitted_order.action).upper())
                commitments[key] += int(float(submitted_order.totalQuantity))
                if reduction_con_id is not None:
                    submitted_tail_sells.add(reduction_con_id)
            elif submitted and order_ref == TAIL_HEDGE_ENTRY_ORDER_REF:
                submitted_tail_entries.add(int(contract.conId))
        self.trades.print_summary()

    async def _reprice_trade(
        self,
        idx: int,
        trade: Any,
        *,
        timeout: float | None = None,
    ) -> bool:
        return await self.execution_manager.reprice_trade(
            self.trades,
            idx,
            trade,
            timeout=timeout,
        )

    async def adjust_prices(self) -> None:
        await self.execution_manager.execute(self.trades)

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
