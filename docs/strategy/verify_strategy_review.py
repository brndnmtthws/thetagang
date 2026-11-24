"""Offline observations of current strategy behavior, not desired-behavior tests.

Run from the repository root: .venv/bin/python -m docs.strategy.verify_strategy_review
All broker access is replaced with local mocks; no IB connection is constructed.
"""

import asyncio
import contextlib
import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import tomlkit
from ib_async import (
    AccountValue,
    Option,
    OptionComputation,
    PortfolioItem,
    Stock,
    Ticker,
)
from rich.table import Table

from thetagang.config import (
    Config,
    RebalanceExecutionPolicyOverride,
    RebalanceMode,
    RunConfig,
    RunStageConfig,
)
from thetagang.portfolio_manager import PortfolioManager
from thetagang.strategies.equity_engine import (
    EquityRebalanceEngine,
    EquityRuntimeServices,
)
from thetagang.strategies.options import (
    OptionsStrategyDeps,
    OptionsWriteService,
    run_option_management_stages,
)
from thetagang.strategies.options_engine import OptionsStrategyEngine
from thetagang.strategies.post_engine import PostStrategyEngine
from thetagang.strategies.regime_engine import RegimeRebalanceEngine
from thetagang.trading_operations import NoValidContractsError, OptionChainScanner


def sample_config() -> Config:
    path = Path(__file__).parents[2] / "thetagang.toml"
    return Config.model_validate(tomlkit.parse(path.read_text()))


def option(symbol: str = "SPY", right: str = "P") -> Option:
    expiry = (date.today() + timedelta(days=60)).strftime("%Y%m%d")
    return Option(symbol, expiry, 100, right, "SMART", multiplier="100")


def account(value: int) -> dict[str, AccountValue]:
    return {
        "NetLiquidation": AccountValue(
            "OFFLINE", "NetLiquidation", str(value), "USD", ""
        )
    }


def holding(contract: Option, quantity: int, pnl: float = 0) -> PortfolioItem:
    return PortfolioItem(contract, quantity, 1, 100 * quantity, 100, pnl, 0, "OFFLINE")


async def observe() -> dict[str, Any]:
    result: dict[str, Any] = {}
    config = sample_config()
    result["sample_switches"] = {
        "stages": [s.id for s in config.run.resolved_stages()],
        "vix_enabled": config.vix_call_hedge.enabled,
        "cash_enabled": config.cash_management.enabled,
    }
    explicit = RunConfig(
        stages=[
            RunStageConfig(id="options_write_calls", kind="options.write_calls"),
            RunStageConfig(id="options_write_puts", kind="options.write_puts"),
        ]
    )
    result["explicit_order_without_depends_on"] = [
        s.id for s in explicit.resolved_stages()
    ]
    assert result["explicit_order_without_depends_on"][0] == "options_write_calls"

    ops = Mock()
    ops.get_order_exchange.return_value = "SMART"
    ops.round_vix_price.side_effect = lambda x: x
    ibkr = Mock()
    vix_contract = option("VIX", "C")
    vix_ticker = Ticker(
        created=True, contract=vix_contract, bid=2, ask=2, bidSize=1, askSize=1
    )
    scanner = Mock()
    scanner.find_eligible_contracts = AsyncMock(return_value=vix_ticker)
    orders = Mock()
    orders.records.return_value = []
    post = PostStrategyEngine(
        config=config,
        ibkr=ibkr,
        order_ops=ops,
        option_scanner=scanner,
        orders=orders,
        qualified_contracts={},
    )
    config.vix_call_hedge.enabled = True
    config.vix_call_hedge.close_hedges_when_vix_exceeds = None
    bands = {}
    for level in [14, 15, 29, 30, 40, 50, 60]:
        ibkr.get_ticker_for_contract = AsyncMock(
            return_value=NS(marketPrice=lambda: level)
        )
        ops.create_limit_order.reset_mock()
        await post.do_vix_hedging(account(100000), {})
        call = ops.create_limit_order.call_args
        bands[str(level)] = call.kwargs["quantity"] if call else 0
    assert bands == {"14": 0, "15": 5, "29": 5, "30": 5, "40": 5, "50": 5, "60": 5}
    result["vix_band_quantities_at_price_2_nlv_100000_close_gate_disabled"] = bands

    config.vix_call_hedge.close_hedges_when_vix_exceeds = 50
    ibkr.get_ticker_for_contract = AsyncMock(
        side_effect=[
            NS(marketPrice=lambda: 60),
            vix_ticker,
            Ticker(
                created=True,
                contract=option("VIX", "P"),
                bid=2,
                ask=2,
                bidSize=1,
                askSize=1,
            ),
        ]
    )
    ops.enqueue_order.reset_mock()
    ops.create_limit_order.side_effect = lambda **kwargs: NS(**kwargs)
    await post.do_vix_hedging(
        {},
        {
            "VIX": [
                holding(vix_contract, 1),
                holding(option("VIX", "P"), -1),
            ]
        },
    )
    close_actions = [
        (c.args[0].right, c.args[1].action) for c in ops.enqueue_order.call_args_list
    ]
    assert close_actions == [("C", "SELL"), ("P", "SELL")]
    result["vix_close_with_long_call_and_short_put"] = close_actions

    cfg = sample_config()
    cfg.portfolio.symbols["SPY"].delta = 0.3
    underlying = Stock("SPY", "SMART", "USD")
    candidate = option("SPY", "C")
    quoted = Ticker(
        created=True,
        contract=candidate,
        bid=2,
        ask=2,
        bidSize=1,
        askSize=1,
        callOpenInterest=20,
    )
    quoted.modelGreeks = OptionComputation(tickAttrib=0, delta=0.6)
    chain_ib = Mock()
    chain_ib.get_ticker_for_contract = AsyncMock(
        return_value=Ticker(
            created=True, contract=underlying, bid=100, ask=100, bidSize=1, askSize=1
        )
    )
    chain_ib.get_chains_for_contract = AsyncMock(
        return_value=[
            NS(
                exchange="SMART",
                tradingClass="SPY",
                strikes=[100],
                expirations=[candidate.lastTradeDateOrContractMonth],
            )
        ]
    )
    chain_ib.qualify_contracts = AsyncMock(return_value=[candidate])
    chain_ib.get_tickers_for_contracts = AsyncMock(return_value=[quoted])
    chain = OptionChainScanner(config=cfg, ibkr=chain_ib, order_ops=ops)
    selected = await chain.find_eligible_contracts(
        underlying, "C", 100, minimum_price=lambda: 0.05, target_delta=0.3
    )
    assert selected.modelGreeks is not None and selected.modelGreeks.delta == 0.6
    result["delta_fallback"] = {"target": 0.3, "selected": 0.6}

    roll = cfg.roll_when
    roll.close_at_pnl = 0.99
    roll.pnl = 0.9
    broker = Mock()
    broker.get_ticker_for_stock = AsyncMock(return_value=NS(marketPrice=lambda: 200))
    old = option()
    position = holding(old, -1, 99.5)
    roll_scanner = Mock()
    engine = OptionsStrategyEngine(
        config=cfg,
        ibkr=broker,
        option_scanner=roll_scanner,
        order_ops=ops,
        services=Mock(),
        target_quantities={},
        has_excess_puts=set(),
        has_excess_calls=set(),
        qualified_contracts={},
    )
    rollable, closeable, _ = await engine.check_puts({"SPY": [position]})
    assert len(rollable) == 1 and not closeable
    result["pnl_0_995_roll_0_9_close_0_99"] = {
        "roll_candidates": len(rollable),
        "close_candidates": len(closeable),
    }
    with (
        patch.object(engine, "close_puts", new_callable=AsyncMock) as close_puts,
        patch.object(engine, "close_calls", new_callable=AsyncMock),
    ):
        await run_option_management_stages(
            OptionsStrategyDeps(
                enabled_stages={"options_close_positions"},
                write_service=cast(OptionsWriteService, engine),
                manage_service=engine,
            ),
            {},
            {"SPY": [position]},
            True,
        )
        assert close_puts.call_args.args == ([],)
    result["close_only_stage_for_roll_eligible_put"] = "no close order"

    cfg.symbols["SPY"].close_if_unable_to_roll = True
    roll_scanner.find_eligible_contracts = AsyncMock(
        side_effect=NoValidContractsError("offline: no eligible replacement")
    )
    broker.get_ticker_for_contract = AsyncMock(
        return_value=Ticker(
            created=True, contract=old, bid=2, ask=2, bidSize=1, askSize=1
        )
    )
    roll.max_dte = None
    no_limit = await engine.roll_positions([position], "P", {})
    roll.max_dte = 180
    with_limit = await engine.roll_positions([position], "P", {})
    assert not no_limit and with_limit == [position]
    result["failed_roll_close_candidates"] = {
        "roll_max_dte_null": len(no_limit),
        "roll_max_dte_180": len(with_limit),
    }
    roll.puts.always_when_itm = True
    roll.max_dte = 1
    broker.get_ticker_for_stock = AsyncMock(return_value=NS(marketPrice=lambda: 50))
    assert await engine.put_can_be_rolled(position, Table())
    result["always_itm_bypasses_roll_max_dte"] = True

    manager = NS(config=cfg, ibkr=chain_ib, get_buying_power=lambda _: 10000)
    chain_ib.get_ticker_for_stock = AsyncMock(
        return_value=Ticker(created=True, bid=100, ask=100, bidSize=1, askSize=1)
    )
    maximum = await PortfolioManager.get_maximum_new_contracts_for(
        cast(PortfolioManager, manager), "SPY", "", {}
    )
    assert maximum == 1
    result["five_percent_of_10000_at_stock_price_100_max_contracts"] = maximum

    regime_cfg = sample_config()
    regime_cfg.regime_rebalance.enabled = True
    regime_cfg.regime_rebalance.symbols = ["SPY", "QQQ"]
    regime_cfg.regime_rebalance.lookback_days = 3
    regime_cfg.regime_rebalance.cooldown_days = 0
    regime_cfg.regime_rebalance.risk.margin_usage = 1
    regime_ib = Mock()
    regime_ib.get_ticker_for_stock = AsyncMock(return_value=NS(marketPrice=lambda: 100))
    regime = RegimeRebalanceEngine(
        config=regime_cfg,
        ibkr=regime_ib,
        order_ops=ops,
        data_store=None,
        get_primary_exchange=lambda _: "",
        get_buying_power=lambda _: 10000,
        now_provider=datetime.now,
    )
    with (
        patch.object(
            regime,
            "_get_regime_proxy_series",
            new=AsyncMock(return_value=([], [1, 1.1, 0.9, 1])),
        ),
        patch.object(
            regime, "_get_last_regime_rebalance_time", new=AsyncMock(return_value=None)
        ),
    ):
        _, rebalance_orders = await regime.check_regime_rebalance_positions(
            account(10000), {}
        )
    quantities = {symbol: qty for symbol, _, qty in rebalance_orders}
    assert quantities == {"SPY": 40, "QQQ": 30}
    result["regime_subset_weights_0_4_0_3_at_base_10000_price_100"] = quantities

    equity_cfg = sample_config()
    equity_cfg.strategies.wheel.equity_rebalance.symbol_overrides["SPY"] = (
        RebalanceExecutionPolicyOverride(mode=RebalanceMode.both)
    )
    equity_cfg.symbols["SPY"].no_trading = True
    equity = EquityRebalanceEngine(
        config=equity_cfg,
        ibkr=regime_ib,
        order_ops=ops,
        services=cast(
            EquityRuntimeServices,
            NS(get_buying_power=lambda _: 10000, get_primary_exchange=lambda _: ""),
        ),
        regime_engine=regime,
    )
    _, buy_orders = await equity.check_buy_only_positions(account(10000), {})
    assert buy_orders == [("SPY", "", 40)]
    result["equity_both_mode_with_no_trading_true"] = {
        "buy_mode": equity_cfg.is_buy_only_rebalancing("SPY"),
        "sell_mode": equity_cfg.is_sell_only_rebalancing("SPY"),
        "planned_buy_shares": buy_orders[0][2],
    }
    return result


def main() -> None:
    # Keep the observation report compact; source methods still execute normally.
    with contextlib.redirect_stdout(io.StringIO()):
        result = asyncio.run(observe())
    target = Path(__file__).with_name("review-observations.json")
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"Saved {target}; all offline observation assertions passed.")


if __name__ == "__main__":
    main()
