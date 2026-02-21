from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

import exchange_calendars as xcals
import numpy as np
import pandas as pd
from ib_async import AccountValue, ExecutionFilter, PortfolioItem, Ticker
from ib_async.contract import Option, Stock
from rich.table import Table

from thetagang import log
from thetagang.config import Config
from thetagang.config_models import RegimeRebalanceBaseEnum
from thetagang.db import DataStore
from thetagang.fmt import dfmt, ffmt, ifmt, pfmt
from thetagang.ibkr import IBKR
from thetagang.strategies.runtime_services import resolve_symbol_configs
from thetagang.trading_operations import OrderOperations


class RegimeRebalanceEngine:
    def __init__(
        self,
        *,
        config: Config,
        ibkr: IBKR,
        order_ops: OrderOperations,
        data_store: Optional[DataStore],
        get_primary_exchange: Callable[[str], str],
        get_buying_power: Callable[[Dict[str, AccountValue]], int],
        now_provider: Callable[[], datetime],
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.order_ops = order_ops
        self.data_store = data_store
        self._get_primary_exchange = get_primary_exchange
        self._get_buying_power = get_buying_power
        self._now = now_provider
        self.regime_rebalance_order_ref_prefix = "tg:regime-rebalance"

    @staticmethod
    def _as_int_or_none(value: Any) -> int | None:
        return value if isinstance(value, int) else None

    @staticmethod
    def _as_float_or_none(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def _resolve_regime_margin_usage(self) -> float:
        fallback_raw = self.config.runtime.account.margin_usage
        fallback = (
            float(fallback_raw)
            if isinstance(fallback_raw, (int, float))
            and not isinstance(fallback_raw, bool)
            else 1.0
        )
        resolver = getattr(self.config, "regime_margin_usage", None)
        if not callable(resolver):
            return fallback
        try:
            resolved = resolver()
        except Exception:
            return fallback
        if isinstance(resolved, bool) or not isinstance(resolved, (int, float)):
            return fallback
        return float(resolved)

    def get_primary_exchange(self, symbol: str) -> str:
        return self._get_primary_exchange(symbol)

    def get_buying_power(self, account_summary: Dict[str, AccountValue]) -> int:
        return self._get_buying_power(account_summary)

    async def _get_regime_proxy_series(
        self,
        symbols: List[str],
        lookback_days: int,
        cooldown_days: int,
        weights_override: Optional[Dict[str, float]] = None,
    ) -> Tuple[List[date], List[float], Dict[str, List[float]]]:
        symbol_configs = resolve_symbol_configs(
            self.config, context="regime proxy series"
        )
        if weights_override:
            symbols = list(weights_override.keys())
        sorted_dates, aligned_closes = await self._get_regime_aligned_closes(
            symbols,
            lookback_days,
            cooldown_days,
        )

        if weights_override:
            weights = weights_override
        else:
            weights = {symbol: symbol_configs[symbol].weight for symbol in symbols}
        total_weight = sum(weights.values())
        if total_weight <= 0:
            log.error("Regime-aware rebalancing weights sum to zero, skipping.")
            raise ValueError(
                "Regime-aware rebalancing weights must sum to a positive value."
            )
        normalized_weights = {
            symbol: weight / total_weight for symbol, weight in weights.items()
        }

        normalized_series = [1.0]
        for idx in range(1, len(sorted_dates)):
            daily_factor = 0.0
            for symbol in symbols:
                prev_close = aligned_closes[symbol][idx - 1]
                curr_close = aligned_closes[symbol][idx]
                daily_factor += normalized_weights[symbol] * (curr_close / prev_close)
            normalized_series.append(normalized_series[-1] * daily_factor)

        return (sorted_dates, normalized_series, aligned_closes)

    async def _get_regime_aligned_closes(
        self,
        symbols: List[str],
        lookback_days: int,
        cooldown_days: int,
    ) -> Tuple[List[date], Dict[str, List[float]]]:
        if not symbols:
            log.error("Regime-aware rebalancing has no symbols to build a proxy.")
            raise ValueError("Regime-aware rebalancing requires proxy symbols.")
        trading_days_needed = lookback_days + 1 + max(cooldown_days, 0)
        calendar_days = math.ceil(trading_days_needed * 7 / 5) + 5
        duration = f"{calendar_days} D"

        async def fetch_history_task(symbol: str) -> Tuple[str, List[Any]]:
            contract = Stock(
                symbol,
                self.order_ops.get_order_exchange(),
                currency="USD",
                primaryExchange=self.get_primary_exchange(symbol),
            )
            bars = await self.ibkr.request_historical_data(contract, duration)
            return symbol, list(bars)

        tasks: List[Coroutine[Any, Any, Tuple[str, List[Any]]]] = [
            fetch_history_task(symbol) for symbol in symbols
        ]
        histories = await log.track_async(
            tasks, description="Fetching regime rebalancing history..."
        )

        closes_by_symbol: Dict[str, Dict[date, float]] = {}
        for symbol, bars in histories:
            closes: Dict[date, float] = {}
            for bar in bars:
                bar_date = bar.date.date() if hasattr(bar.date, "date") else bar.date
                closes[bar_date] = float(bar.close)
            closes_by_symbol[symbol] = closes

        common_dates = set.intersection(
            *(set(closes.keys()) for closes in closes_by_symbol.values())
        )
        if not common_dates:
            log.error(
                "Regime-aware rebalancing history has no common dates across symbols."
            )
            raise ValueError(
                "Regime-aware rebalancing requires aligned history for all symbols."
            )

        sorted_dates = sorted(common_dates)
        if len(sorted_dates) < 2:
            log.error("Regime-aware rebalancing history has fewer than 2 points.")
            raise ValueError(
                "Regime-aware rebalancing requires at least 2 history points."
            )

        aligned_closes: Dict[str, List[float]] = {}
        for symbol in symbols:
            aligned: List[float] = []
            for date_point in sorted_dates:
                close = closes_by_symbol[symbol].get(date_point)
                if close is None or math.isnan(close) or math.isclose(close, 0):
                    log.error(
                        f"Invalid close for {symbol} on {date_point} (close={close})."
                    )
                    raise ValueError(
                        "Regime-aware rebalancing found invalid historical closes."
                    )
                aligned.append(close)
            aligned_closes[symbol] = aligned

        return (sorted_dates, aligned_closes)

    async def _get_last_regime_rebalance_time(
        self, symbols: List[str]
    ) -> Optional[datetime]:
        regime_rebalance = self.config.strategies.regime_rebalance
        if not regime_rebalance.enabled:
            return None

        lookback_days = max(regime_rebalance.order_history_lookback_days, 1)
        start_time = self._now() - timedelta(days=lookback_days)
        exec_filter = ExecutionFilter(time=start_time.strftime("%Y%m%d %H:%M:%S"))

        if self.data_store:
            fills = await self.ibkr.request_executions(exec_filter)
            self.data_store.record_executions(fills)
            return self.data_store.get_last_regime_rebalance_time(
                symbols,
                self.regime_rebalance_order_ref_prefix,
                start_time,
            )

        fills = await self.ibkr.request_executions(exec_filter)
        last_rebalance: Optional[datetime] = None
        for fill in fills:
            execution = fill.execution
            if not execution.orderRef:
                continue
            if not execution.orderRef.startswith(
                self.regime_rebalance_order_ref_prefix
            ):
                continue
            if fill.contract.symbol not in symbols:
                continue
            fill_time = fill.time or execution.time
            if last_rebalance is None or fill_time > last_rebalance:
                last_rebalance = fill_time

        return last_rebalance

    def _cooldown_elapsed(self, last_rebalance: datetime, cooldown_days: int) -> bool:
        if cooldown_days <= 0:
            return True

        now = self._now()
        if last_rebalance >= now:
            return False

        start_date = last_rebalance.date()
        end_date = now.date()
        if end_date < start_date:
            return False

        try:
            exchange = self.config.runtime.exchange_hours.exchange
            calendar = xcals.get_calendar(exchange)
            start_ts = pd.Timestamp(start_date)
            end_ts = pd.Timestamp(end_date)
            sessions = calendar.sessions
            sessions = sessions[(sessions >= start_ts) & (sessions <= end_ts)]
            if sessions.empty:
                raise ValueError("No exchange sessions found in cooldown window.")
            session_dates = [session.date() for session in sessions]
            sessions_after = [d for d in session_dates if d > start_date]
            return len(sessions_after) >= cooldown_days
        except Exception as exc:
            log.warning(
                "Regime rebalancing cooldown calculation failed "
                f"({type(exc).__name__}); using calendar days."
            )
            return (end_date - start_date).days >= cooldown_days

    async def check_regime_rebalance_positions(
        self,
        account_summary: Dict[str, AccountValue],
        portfolio_positions: Dict[str, List[PortfolioItem]],
    ) -> Tuple[Table, List[Tuple[str, str, int]]]:
        symbol_configs = resolve_symbol_configs(
            self.config, context="regime rebalance check"
        )
        table = Table(title="Regime-aware rebalancing summary")
        table.add_column("Symbol")
        table.add_column("Weights", justify="right")
        table.add_column("Value", justify="right")
        table.add_column("Shares", justify="right")
        table.add_column("Gate", justify="center")
        table.add_column("Action")

        to_trade: List[Tuple[str, str, int]] = []
        regime_rebalance = self.config.strategies.regime_rebalance
        if not regime_rebalance.enabled:
            return (table, to_trade)

        symbols = list(regime_rebalance.symbols)
        if not symbols:
            log.warning(
                "Regime-aware rebalancing enabled but no symbols are configured."
            )
            return (table, to_trade)

        missing_symbols = [symbol for symbol in symbols if symbol not in symbol_configs]
        if missing_symbols:
            log.error(
                f"Regime-aware rebalancing symbols missing from config: {', '.join(missing_symbols)}"
            )
            raise ValueError(
                "Regime-aware rebalancing requires symbols present in config."
            )

        zero_weight_symbols = [
            symbol for symbol in symbols if symbol_configs[symbol].weight <= 0
        ]
        if zero_weight_symbols:
            log.warning(
                "Regime-aware rebalancing ignoring zero-weight symbols: "
                f"{', '.join(zero_weight_symbols)}"
            )
        symbols = [symbol for symbol in symbols if symbol_configs[symbol].weight > 0]
        if not symbols:
            log.error("Regime-aware rebalancing has no positive-weight symbols.")
            raise ValueError(
                "Regime-aware rebalancing requires positive target weights."
            )

        stock_positions = [
            position
            for symbol in portfolio_positions
            for position in portfolio_positions[symbol]
            if isinstance(position.contract, Stock)
        ]
        stock_symbols: Dict[str, PortfolioItem] = {
            position.contract.symbol: position for position in stock_positions
        }

        async def get_ticker_task(symbol: str) -> Tuple[str, Ticker]:
            ticker = await self.ibkr.get_ticker_for_stock(
                symbol, self.get_primary_exchange(symbol)
            )
            return symbol, ticker

        ticker_tasks: List[Coroutine[Any, Any, Tuple[str, Ticker]]] = [
            get_ticker_task(symbol) for symbol in symbols
        ]
        ticker_results = await log.track_async(
            ticker_tasks, description="Fetching regime rebalancing prices..."
        )
        tickers = {symbol: ticker for symbol, ticker in ticker_results}

        current_positions: Dict[str, int] = {}
        current_values: Dict[str, float] = {}
        market_prices: Dict[str, float] = {}
        target_shares: Dict[str, int] = {}
        target_values: Dict[str, float] = {}
        relative_ratios: Dict[str, float] = {}
        relative_drifts: Dict[str, float] = {}
        share_gaps: Dict[str, int] = {}
        for symbol in symbols:
            ticker = tickers[symbol]
            market_price = ticker.marketPrice()
            if (
                not market_price
                or math.isnan(market_price)
                or math.isclose(market_price, 0)
            ):
                log.error(
                    f"Invalid market price for {symbol} (market_price={market_price}), skipping for now"
                )
                raise ValueError(
                    "Regime-aware rebalancing requires valid market prices."
                )
            market_prices[symbol] = market_price

            current_position = math.floor(
                stock_symbols[symbol].position if symbol in stock_symbols else 0
            )
            current_positions[symbol] = current_position
            current_value = current_position * market_price
            current_values[symbol] = current_value

        weight_base = regime_rebalance.weight_base
        regime_margin_usage = self._resolve_regime_margin_usage()
        if weight_base == RegimeRebalanceBaseEnum.managed_stocks:
            total_value = sum(current_values.values())
        elif weight_base == RegimeRebalanceBaseEnum.net_liq_ex_options:
            excluded_value = 0.0
            for positions in portfolio_positions.values():
                for position in positions:
                    if isinstance(position.contract, Option):
                        excluded_value += float(position.marketValue or 0.0)
            net_liq = float(account_summary["NetLiquidation"].value)
            adjusted_net_liq = net_liq - excluded_value
            total_value = math.floor(adjusted_net_liq * regime_margin_usage)
            log.notice(
                "Regime rebalancing base: mode=net_liq_ex_options "
                f"net_liq={dfmt(net_liq)} excluded_options={dfmt(excluded_value)} "
                f"margin_usage={ffmt(regime_margin_usage)} "
                f"base={dfmt(total_value)}"
            )
        else:
            total_value = self.get_buying_power(account_summary)
        if total_value <= 0:
            log.error("Rebalance base value is not positive, skipping rebalancing.")
            raise ValueError("Regime-aware rebalancing requires a positive base value.")

        current_weights: Dict[str, float] = {}
        for symbol in symbols:
            market_price = market_prices[symbol]
            current_position = current_positions[symbol]
            current_value = current_values[symbol]
            current_weights[symbol] = current_value / total_value
            target_weight = symbol_configs[symbol].weight
            target_values[symbol] = target_weight * total_value
            target_shares[symbol] = math.floor(target_values[symbol] / market_price)
            share_gaps[symbol] = target_shares[symbol] - current_position
            relative_ratio = current_weights[symbol] / target_weight
            relative_ratios[symbol] = relative_ratio
            relative_drifts[symbol] = abs(relative_ratio - 1.0)

        invested_value = sum(current_values.values())
        proxy_symbols = [symbol for symbol in symbols if current_values[symbol] > 0]
        proxy_weights: Dict[str, float] = {}
        if proxy_symbols:
            proxy_invested = sum(current_values[symbol] for symbol in proxy_symbols)
            proxy_weights = {
                symbol: current_values[symbol] / proxy_invested
                for symbol in proxy_symbols
            }
        else:
            log.warning(
                "Regime proxy has no invested symbols; falling back to target weights."
            )
            proxy_weights = {
                symbol: symbol_configs[symbol].weight for symbol in symbols
            }

        dates, values, aligned_closes = await self._get_regime_proxy_series(
            symbols,
            regime_rebalance.lookback_days,
            regime_rebalance.cooldown_days,
            weights_override=proxy_weights,
        )
        if len(values) < regime_rebalance.lookback_days + 1:
            log.error("Insufficient historical data for regime rebalancing, aborting.")
            raise ValueError("Regime-aware rebalancing requires full lookback history.")

        window = np.array(values[-(regime_rebalance.lookback_days + 1) :])
        safe_prev = np.maximum(window[:-1], regime_rebalance.eps)
        safe_curr = np.maximum(window[1:], regime_rebalance.eps)
        r = np.log(safe_curr / safe_prev)
        sigma = math.sqrt(float(np.sum(r * r)))
        disp = abs(float(np.sum(r)))
        choppiness = sigma / max(disp, regime_rebalance.eps)
        chop_ok = choppiness >= regime_rebalance.choppiness_min

        diffs = np.abs(np.diff(window))
        efficiency = abs(float(window[-1] - window[0])) / max(
            float(np.sum(diffs)), regime_rebalance.eps
        )
        er_ok = efficiency <= regime_rebalance.efficiency_max
        regime_ok = chop_ok and er_ok

        ratio_gate = getattr(regime_rebalance, "ratio_gate", None)
        ratio_ok: Optional[bool] = None
        ratio_var: Optional[float] = None
        ratio_tstat: Optional[float] = None
        ratio_var_threshold: Optional[float] = None
        ratio_drift_max: Optional[float] = None
        ratio_anchor: Optional[str] = None
        ratio_rest: List[str] = []
        if ratio_gate is not None:
            ratio_anchor = getattr(ratio_gate, "anchor", "")
            ratio_rest = [s for s in symbols if s != ratio_anchor]
            if not ratio_anchor or ratio_anchor not in symbols or not ratio_rest:
                log.error("Regime-aware ratio gate has invalid anchor configuration.")
                raise ValueError(
                    "Regime-aware ratio gate requires a valid anchor and rest basket."
                )

            rest_weights = {
                symbol: symbol_configs[symbol].weight for symbol in ratio_rest
            }
            total_rest_weight = sum(rest_weights.values())
            if total_rest_weight <= 0:
                log.error("Ratio gate rest weights sum to zero, skipping.")
                raise ValueError("Regime-aware ratio gate requires positive weights.")
            normalized_rest_weights = {
                symbol: weight / total_rest_weight
                for symbol, weight in rest_weights.items()
            }

            rest_index = []
            anchor_series = aligned_closes[ratio_anchor]
            for idx in range(len(dates)):
                basket_value = 0.0
                for symbol in ratio_rest:
                    basket_value += (
                        normalized_rest_weights[symbol] * aligned_closes[symbol][idx]
                    )
                rest_index.append(max(basket_value, regime_rebalance.eps))

            anchor_prices = [
                max(price, regime_rebalance.eps) for price in anchor_series
            ]
            ratio_series = np.log(np.array(rest_index) / np.array(anchor_prices))
            ratio_returns = pd.Series(ratio_series).diff()
            ratio_var = float(
                ratio_returns.rolling(regime_rebalance.lookback_days)
                .var(ddof=1)
                .iloc[-1]
            )
            ratio_mean = float(
                ratio_returns.rolling(regime_rebalance.lookback_days).mean().iloc[-1]
            )
            ratio_std = float(
                ratio_returns.rolling(regime_rebalance.lookback_days)
                .std(ddof=1)
                .iloc[-1]
            )
            if math.isnan(ratio_var) or math.isnan(ratio_mean) or math.isnan(ratio_std):
                ratio_ok = False
                ratio_tstat = float("inf")
                ratio_var_threshold = max(
                    float(getattr(ratio_gate, "var_min", 0.0)), 0.0
                )
                ratio_drift_max = float(getattr(ratio_gate, "drift_max", 0.0))
            else:
                if ratio_std <= 0:
                    ratio_tstat = float("inf")
                else:
                    ratio_tstat = abs(
                        ratio_mean
                        / (ratio_std / math.sqrt(regime_rebalance.lookback_days))
                    )

                ratio_var_threshold = max(
                    float(getattr(ratio_gate, "var_min", 0.0)), 0.0
                )
                ratio_drift_max = float(getattr(ratio_gate, "drift_max", 0.0))
                ratio_ok = (
                    ratio_var >= ratio_var_threshold and ratio_tstat <= ratio_drift_max
                )

        last_rebalance = await self._get_last_regime_rebalance_time(symbols)
        cooldown_ok = True
        if last_rebalance and regime_rebalance.cooldown_days > 0:
            cooldown_ok = self._cooldown_elapsed(
                last_rebalance, regime_rebalance.cooldown_days
            )

        soft_breach = any(
            drift + regime_rebalance.eps >= regime_rebalance.soft_band
            for drift in relative_drifts.values()
        )
        hard_breach = any(
            drift + regime_rebalance.eps >= regime_rebalance.hard_band
            for drift in relative_drifts.values()
        )

        max_relative_drift = max(relative_drifts.values()) if relative_drifts else 0.0
        hard_rebalance = hard_breach
        ratio_enabled = (
            bool(getattr(ratio_gate, "enabled", False)) if ratio_gate else False
        )
        ratio_gate_ok = (
            True if ratio_gate is None or not ratio_enabled else bool(ratio_ok)
        )
        soft_rebalance = soft_breach and regime_ok and cooldown_ok and ratio_gate_ok
        rebalance_fraction = 1.0
        if hard_rebalance:
            rebalance_fraction = regime_rebalance.hard_band_rebalance_fraction

        share_tolerance = 1
        flow_active = False
        deficit_active = False
        if self.data_store:
            state = self.data_store.get_last_event_payload("regime_rebalance_state")
            if state:
                flow_active = bool(state.get("flow_active", False))
                deficit_active = bool(state.get("deficit_active", False))

        excess_cash = total_value - invested_value
        flow_trade_min_amount = total_value * regime_rebalance.flow_trade_min
        flow_trade_stop_amount = total_value * regime_rebalance.flow_trade_stop
        deficit_rail_start_amount = total_value * regime_rebalance.deficit_rail_start
        deficit_rail_stop_amount = total_value * regime_rebalance.deficit_rail_stop
        flow_gate = False
        deficit_gate = False
        if excess_cash < 0:
            deficit_amount = -excess_cash
            deficit_gate = deficit_amount >= deficit_rail_start_amount or (
                deficit_active and deficit_amount >= deficit_rail_stop_amount
            )
            if not deficit_gate:
                flow_gate = deficit_amount >= flow_trade_min_amount or (
                    flow_active and deficit_amount >= flow_trade_stop_amount
                )
        else:
            flow_gate = excess_cash >= flow_trade_min_amount or (
                flow_active and excess_cash >= flow_trade_stop_amount
            )

        allowed_symbols = {
            symbol for symbol in symbols if self.config.trading_is_allowed(symbol)
        }

        def build_flow_orders(amount: float) -> Dict[str, int]:
            if amount == 0:
                return {}
            active_symbols = [
                symbol
                for symbol in symbols
                if symbol in allowed_symbols
                and abs(share_gaps[symbol]) > share_tolerance
            ]
            if not active_symbols:
                return {}
            net_gap = sum(share_gaps[symbol] for symbol in active_symbols)
            tot_gap = sum(abs(share_gaps[symbol]) for symbol in active_symbols)
            if tot_gap <= 0:
                return {}

            ok_buy = net_gap > regime_rebalance.flow_imbalance_tau * tot_gap
            ok_sell = net_gap < -regime_rebalance.flow_imbalance_tau * tot_gap
            if amount > 0 and not ok_buy:
                return {}
            if amount < 0 and not ok_sell:
                return {}

            orders: Dict[str, int] = {}
            if amount > 0:
                deficits = {
                    symbol: max(share_gaps[symbol], 0) for symbol in active_symbols
                }
                total_deficit = sum(deficits.values())
                if total_deficit <= 0:
                    return {}
                for symbol in active_symbols:
                    deficit = deficits[symbol]
                    if deficit <= 0:
                        continue
                    if not self.config.trading_is_allowed(symbol):
                        continue
                    max_buy = max(
                        (target_shares[symbol] + share_tolerance)
                        - current_positions[symbol],
                        0,
                    )
                    if max_buy <= 0:
                        continue
                    alloc = amount * (deficit / total_deficit)
                    buy_shares = min(int(alloc // market_prices[symbol]), max_buy)
                    if buy_shares > 0:
                        orders[symbol] = buy_shares
            else:
                need = -amount
                excesses = {
                    symbol: max(-share_gaps[symbol], 0) for symbol in active_symbols
                }
                total_excess = sum(excesses.values())
                if total_excess <= 0:
                    return {}
                for symbol in active_symbols:
                    excess = excesses[symbol]
                    if excess <= 0:
                        continue
                    if not self.config.trading_is_allowed(symbol):
                        continue
                    max_sell = max(
                        current_positions[symbol]
                        - max(target_shares[symbol] - share_tolerance, 0),
                        0,
                    )
                    if max_sell <= 0:
                        continue
                    alloc = need * (excess / total_excess)
                    sell_shares = min(
                        math.ceil(alloc / market_prices[symbol]), max_sell
                    )
                    if sell_shares > 0:
                        orders[symbol] = -sell_shares
            return orders

        def build_deficit_orders(
            shares_state: Dict[str, int],
            amount: float,
            allow_below_target: bool,
            allowed_symbols: set[str],
        ) -> Dict[str, int]:
            if amount <= 0:
                return {}
            orders: Dict[str, int] = {}
            initial_amount = amount

            overweight_symbols = [
                symbol
                for symbol in symbols
                if shares_state[symbol] > target_shares[symbol] + share_tolerance
                and symbol in allowed_symbols
            ]
            if overweight_symbols:
                overages = {
                    symbol: max(
                        shares_state[symbol]
                        - (target_shares[symbol] + share_tolerance),
                        0,
                    )
                    for symbol in overweight_symbols
                }
                total_over = sum(overages.values())
                for symbol in overweight_symbols:
                    over = overages[symbol]
                    if over <= 0:
                        continue
                    max_sell = max(
                        shares_state[symbol]
                        - max(target_shares[symbol] - share_tolerance, 0),
                        0,
                    )
                    if max_sell <= 0:
                        continue
                    alloc = (
                        initial_amount * (over / total_over)
                        if total_over > 0
                        else amount
                    )
                    alloc = min(alloc, amount)
                    sell_shares = min(
                        math.ceil(alloc / market_prices[symbol]), max_sell
                    )
                    if sell_shares > 0:
                        orders[symbol] = orders.get(symbol, 0) - sell_shares
                        amount -= sell_shares * market_prices[symbol]
                        if amount <= 0:
                            return orders

            if not allow_below_target:
                return orders

            while amount > 0:
                any_sold = False
                for symbol in symbols:
                    if symbol_configs[symbol].weight <= 0:
                        continue
                    if symbol not in allowed_symbols:
                        continue
                    max_sell = shares_state[symbol] + orders.get(symbol, 0)
                    if max_sell <= 0:
                        continue
                    alloc = amount * symbol_configs[symbol].weight
                    sell_shares = min(
                        math.ceil(alloc / market_prices[symbol]), max_sell
                    )
                    if sell_shares <= 0:
                        continue
                    orders[symbol] = orders.get(symbol, 0) - sell_shares
                    amount -= sell_shares * market_prices[symbol]
                    any_sold = True
                    if amount <= 0:
                        break
                if not any_sold:
                    break
            return orders

        orders_by_symbol: Dict[str, int] = {}
        rebalance_mode = "no"
        deficit_gate_after = False
        if hard_rebalance or soft_rebalance:
            rebalance_mode = "hard" if hard_rebalance else "soft"
            for symbol in symbols:
                desired = target_shares[symbol] - current_positions[symbol]
                if hard_rebalance and not math.isclose(rebalance_fraction, 1.0):
                    desired = int(round(desired * rebalance_fraction))
                if desired == 0:
                    continue
                if symbol in allowed_symbols:
                    orders_by_symbol[symbol] = orders_by_symbol.get(symbol, 0) + desired

            shares_after = {
                symbol: current_positions[symbol] + orders_by_symbol.get(symbol, 0)
                for symbol in symbols
            }
            invested_after = sum(
                shares_after[symbol] * market_prices[symbol] for symbol in symbols
            )
            excess_after = total_value - invested_after
            deficit_amount_after = max(0.0, -excess_after)
            deficit_gate_after = deficit_amount_after >= deficit_rail_stop_amount
            if deficit_gate_after:
                deficit_needed = max(
                    0.0, deficit_amount_after - deficit_rail_stop_amount
                )
                deficit_orders = build_deficit_orders(
                    shares_after,
                    deficit_needed,
                    allow_below_target=True,
                    allowed_symbols=allowed_symbols,
                )
                if deficit_orders:
                    rebalance_mode = f"{rebalance_mode}+deficit"
                    for symbol, delta in deficit_orders.items():
                        if delta == 0:
                            continue
                        orders_by_symbol[symbol] = (
                            orders_by_symbol.get(symbol, 0) + delta
                        )
        elif deficit_gate:
            rebalance_mode = "deficit"
            deficit_needed = max(0.0, -excess_cash - deficit_rail_stop_amount)
            deficit_orders = build_deficit_orders(
                current_positions,
                deficit_needed,
                allow_below_target=True,
                allowed_symbols=allowed_symbols,
            )
            if deficit_orders:
                for symbol, delta in deficit_orders.items():
                    if delta == 0:
                        continue
                    orders_by_symbol[symbol] = orders_by_symbol.get(symbol, 0) + delta
        elif flow_gate:
            rebalance_mode = "flow"
            flow_orders = build_flow_orders(excess_cash)
            for symbol, delta in flow_orders.items():
                if delta == 0:
                    continue
                orders_by_symbol[symbol] = orders_by_symbol.get(symbol, 0) + delta
        regime_summary: List[Dict[str, Any]] = []
        net_liquidation_value = float(account_summary["NetLiquidation"].value)
        for symbol in symbols:
            target_weight = symbol_configs[symbol].weight
            target_value = target_values[symbol]
            target_share = target_shares[symbol]
            trade_shares = orders_by_symbol.get(symbol, 0)
            filtered_trade_shares = trade_shares
            trading_allowed = self.config.trading_is_allowed(symbol)
            rebalance_policy_fn = getattr(self.config, "regime_rebalance_policy", None)
            rebalance_policy = (
                rebalance_policy_fn(symbol) if callable(rebalance_policy_fn) else None
            )
            allows_buy = (
                rebalance_policy.allows_buy() if rebalance_policy is not None else True
            )
            allows_sell = (
                rebalance_policy.allows_sell() if rebalance_policy is not None else True
            )
            mode_value = (
                rebalance_policy.mode.value if rebalance_policy is not None else "both"
            )

            if filtered_trade_shares > 0 and not allows_buy:
                filtered_trade_shares = 0
                action = f"[cyan]Skip (mode={mode_value})"
            elif filtered_trade_shares < 0 and not allows_sell:
                filtered_trade_shares = 0
                action = f"[cyan]Skip (mode={mode_value})"
            elif filtered_trade_shares != 0:
                trade_abs = abs(filtered_trade_shares)
                trade_amount = trade_abs * market_prices[symbol]
                symbol_config = symbol_configs[symbol]
                min_shares = (
                    self._as_int_or_none(
                        getattr(rebalance_policy, "min_threshold_shares", None)
                    )
                    or self._as_int_or_none(
                        getattr(symbol_config, "buy_only_min_threshold_shares", None)
                    )
                    or self._as_int_or_none(
                        getattr(symbol_config, "sell_only_min_threshold_shares", None)
                    )
                    or 1
                )
                min_amount = self._as_float_or_none(
                    getattr(rebalance_policy, "min_threshold_amount", None)
                )
                if min_amount is None:
                    min_amount = self._as_float_or_none(
                        getattr(symbol_config, "buy_only_min_threshold_amount", None)
                    )
                if min_amount is None:
                    min_amount = self._as_float_or_none(
                        getattr(symbol_config, "sell_only_min_threshold_amount", None)
                    )

                min_percent = self._as_float_or_none(
                    getattr(rebalance_policy, "min_threshold_percent", None)
                )
                if min_percent is None:
                    min_percent = self._as_float_or_none(
                        getattr(symbol_config, "buy_only_min_threshold_percent", None)
                    )
                if min_percent is None:
                    min_percent = self._as_float_or_none(
                        getattr(symbol_config, "sell_only_min_threshold_percent", None)
                    )

                min_percent_relative = self._as_float_or_none(
                    getattr(rebalance_policy, "min_threshold_percent_relative", None)
                )
                if min_percent_relative is None:
                    min_percent_relative = self._as_float_or_none(
                        getattr(
                            symbol_config,
                            "buy_only_min_threshold_percent_relative",
                            None,
                        )
                    )
                if min_percent_relative is None:
                    min_percent_relative = self._as_float_or_none(
                        getattr(
                            symbol_config,
                            "sell_only_min_threshold_percent_relative",
                            None,
                        )
                    )

                if min_percent is not None:
                    percent_min_amount = net_liquidation_value * min_percent
                    min_amount = (
                        max(min_amount, percent_min_amount)
                        if min_amount is not None
                        else percent_min_amount
                    )

                relative_diff = 0.0
                if target_value > 0:
                    if filtered_trade_shares > 0:
                        relative_diff = (
                            target_value - current_values[symbol]
                        ) / target_value
                    else:
                        relative_diff = (
                            current_values[symbol] - target_value
                        ) / target_value

                if trade_abs < min_shares:
                    filtered_trade_shares = 0
                    action = f"[yellow]Skip (below min shares {min_shares})"
                elif min_amount is not None and trade_amount < min_amount:
                    filtered_trade_shares = 0
                    action = (
                        f"[yellow]Skip (below min amount {dfmt(min_amount)}; "
                        f"would be {dfmt(trade_amount)})"
                    )
                elif (
                    min_percent_relative is not None
                    and target_value > 0
                    and relative_diff < min_percent_relative
                ):
                    filtered_trade_shares = 0
                    action = f"[yellow]Skip (below relative threshold {pfmt(min_percent_relative)})"

            if filtered_trade_shares != 0:
                to_trade.append(
                    (
                        symbol,
                        self.get_primary_exchange(symbol),
                        filtered_trade_shares,
                    )
                )
                action = (
                    f"[green]Buy {filtered_trade_shares}"
                    if filtered_trade_shares > 0
                    else f"[green]Sell {abs(filtered_trade_shares)}"
                )
            elif not trading_allowed:
                action = "[cyan]Skip (no_trading)"
            elif filtered_trade_shares == trade_shares:
                action = "[cyan]Hold"

            weight_delta = current_weights[symbol] - target_weight
            value_delta = current_values[symbol] - target_value
            shares_delta = current_positions[symbol] - target_share
            band_status = "hard" if hard_breach else "soft" if soft_breach else "no"
            gate_status = (
                f"mode={rebalance_mode} "
                f"band={band_status} "
                f"regime={'ok' if regime_ok else 'no'} "
                f"cooldown={'ok' if cooldown_ok else 'no'} "
                f"flow={'on' if flow_gate else 'off'} "
                f"deficit={'on' if deficit_gate else 'off'}"
            )

            table.add_row(
                symbol,
                f"{pfmt(current_weights[symbol])}->{pfmt(target_weight)} "
                f"({pfmt(weight_delta)})",
                f"{dfmt(current_values[symbol])}->{dfmt(target_value)} "
                f"({dfmt(value_delta)})",
                f"{ifmt(current_positions[symbol])}->{ifmt(target_share)} "
                f"({ifmt(shares_delta)})",
                gate_status,
                action,
            )
            regime_summary.append(
                {
                    "symbol": symbol,
                    "market_price": market_prices[symbol],
                    "current_weight": current_weights[symbol],
                    "target_weight": target_weight,
                    "current_value": current_values[symbol],
                    "target_value": target_value,
                    "current_shares": current_positions[symbol],
                    "target_shares": target_share,
                    "shares_to_trade": filtered_trade_shares,
                    "weight_delta": weight_delta,
                    "value_delta": value_delta,
                    "shares_delta": shares_delta,
                    "trading_allowed": trading_allowed,
                    "action": action,
                }
            )

        log.info(
            f"Regime rebalancing gates: max_relative_drift={pfmt(max_relative_drift)} "
            f"soft_band={pfmt(regime_rebalance.soft_band, 0)} "
            f"hard_band={pfmt(regime_rebalance.hard_band, 0)} "
            f"hard_breach={hard_breach} soft_breach={soft_breach} "
            f"chop={ffmt(choppiness)} er={pfmt(efficiency)} "
            f"cooldown_ok={cooldown_ok} mode={rebalance_mode} "
            f"flow_gate={flow_gate} deficit_gate={deficit_gate} "
            f"flow_active={flow_active} deficit_active={deficit_active} "
            f"flow_min={pfmt(regime_rebalance.flow_trade_min)}({dfmt(flow_trade_min_amount)}) "
            f"flow_stop={pfmt(regime_rebalance.flow_trade_stop)}({dfmt(flow_trade_stop_amount)}) "
            f"deficit_start={pfmt(regime_rebalance.deficit_rail_start)}({dfmt(deficit_rail_start_amount)}) "
            f"deficit_stop={pfmt(regime_rebalance.deficit_rail_stop)}({dfmt(deficit_rail_stop_amount)}) "
            f"excess_cash={dfmt(excess_cash)}"
            + (
                " "
                + "ratio_gate="
                + ("on" if ratio_enabled else "shadow")
                + f" ratio_ok={ratio_ok} "
                + f"ratio_var={ffmt(ratio_var) if ratio_var is not None else '-'} "
                + f"ratio_var_min={ffmt(ratio_var_threshold) if ratio_var_threshold is not None else '-'} "
                + f"ratio_tstat={ffmt(ratio_tstat) if ratio_tstat is not None else '-'} "
                + f"ratio_drift_max={ffmt(ratio_drift_max) if ratio_drift_max is not None else '-'} "
                + f"anchor={ratio_anchor} rest={','.join(ratio_rest)}"
                if ratio_gate is not None
                else ""
            )
        )
        if self.data_store:
            ratio_payload = None
            if ratio_gate is not None:
                ratio_payload = {
                    "enabled": ratio_enabled,
                    "anchor": ratio_anchor,
                    "rest": ratio_rest,
                    "var": ratio_var,
                    "var_min": ratio_var_threshold,
                    "tstat": ratio_tstat,
                    "drift_max": ratio_drift_max,
                    "ok": ratio_ok,
                }
            deficit_active_state = (
                deficit_gate_after
                if (hard_rebalance or soft_rebalance)
                else deficit_gate
            )
            self.data_store.record_event(
                "regime_rebalance_gate",
                {
                    "symbols": symbols,
                    "max_relative_drift": max_relative_drift,
                    "soft_band": regime_rebalance.soft_band,
                    "hard_band": regime_rebalance.hard_band,
                    "hard_breach": hard_breach,
                    "soft_breach": soft_breach,
                    "choppiness": choppiness,
                    "efficiency": efficiency,
                    "cooldown_ok": cooldown_ok,
                    "flow_gate": flow_gate,
                    "deficit_gate": deficit_gate,
                    "excess_cash": excess_cash,
                    "mode": rebalance_mode,
                    "orders": to_trade,
                    "ratio_gate": ratio_payload,
                },
            )
            self.data_store.record_event(
                "regime_rebalance_summary",
                {
                    "symbols": symbols,
                    "total_value": total_value,
                    "hard_breach": hard_breach,
                    "soft_breach": soft_breach,
                    "regime_ok": regime_ok,
                    "cooldown_ok": cooldown_ok,
                    "flow_gate": flow_gate,
                    "deficit_gate": deficit_gate,
                    "excess_cash": excess_cash,
                    "mode": rebalance_mode,
                    "summary": regime_summary,
                    "ratio_gate": ratio_payload,
                },
            )
            self.data_store.record_event(
                "regime_rebalance_state",
                {
                    "flow_active": rebalance_mode == "flow" and flow_gate,
                    "deficit_active": deficit_active_state,
                },
            )

        return (table, to_trade)
