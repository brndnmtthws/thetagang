from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from thetagang.strategies.equity import (
    EquityRebalanceService,
    EquityStrategyDeps,
    RegimeRebalanceService,
    run_equity_rebalance_stages,
)
from thetagang.strategies.options import (
    OptionsManageService,
    OptionsStrategyDeps,
    OptionsWriteService,
    run_option_management_stages,
    run_option_write_stages,
)
from thetagang.strategies.post import (
    PostStageService,
    PostStrategyDeps,
    run_post_stages,
)


@pytest.mark.asyncio
async def test_run_option_write_stages_skips_when_options_disabled(mocker):
    write_service = SimpleNamespace(
        check_if_can_write_puts=AsyncMock(),
        write_puts=AsyncMock(),
        check_for_uncovered_positions=AsyncMock(),
        write_calls=AsyncMock(),
    )
    deps = OptionsStrategyDeps(
        enabled_stages={"options_write_puts", "options_write_calls"},
        write_service=cast(OptionsWriteService, write_service),
        manage_service=cast(OptionsManageService, SimpleNamespace()),
    )

    await run_option_write_stages(deps, {}, {}, options_enabled=False)

    write_service.check_if_can_write_puts.assert_not_called()
    write_service.check_for_uncovered_positions.assert_not_called()
    write_service.write_puts.assert_not_called()
    write_service.write_calls.assert_not_called()


@pytest.mark.asyncio
async def test_run_option_write_stages_puts_and_calls_paths(mocker):
    positions_table = object()
    put_actions_table = object()
    call_actions_table = object()
    write_service = SimpleNamespace(
        check_if_can_write_puts=AsyncMock(
            return_value=(positions_table, put_actions_table, ["PUT_ORDER"])
        ),
        write_puts=AsyncMock(),
        check_for_uncovered_positions=AsyncMock(
            return_value=(call_actions_table, ["CALL_ORDER"])
        ),
        write_calls=AsyncMock(),
    )
    deps = OptionsStrategyDeps(
        enabled_stages={"options_write_puts", "options_write_calls"},
        write_service=cast(OptionsWriteService, write_service),
        manage_service=cast(OptionsManageService, SimpleNamespace()),
    )
    print_mock = mocker.patch("thetagang.strategies.options.log.print")

    await run_option_write_stages(deps, {}, {}, options_enabled=True)

    write_service.check_if_can_write_puts.assert_awaited_once()
    write_service.write_puts.assert_awaited_once_with(["PUT_ORDER"])
    write_service.check_for_uncovered_positions.assert_awaited_once()
    write_service.write_calls.assert_awaited_once_with(["CALL_ORDER"])
    assert print_mock.call_count == 3


@pytest.mark.asyncio
async def test_run_option_management_stages_roll_only(mocker):
    manage_service = SimpleNamespace(
        check_puts=AsyncMock(return_value=(["RP"], ["CP"], "g1")),
        check_calls=AsyncMock(return_value=(["RC"], ["CC"], "g2")),
        roll_puts=AsyncMock(return_value=[]),
        roll_calls=AsyncMock(return_value=[]),
        close_puts=AsyncMock(),
        close_calls=AsyncMock(),
    )
    deps = OptionsStrategyDeps(
        enabled_stages={"options_roll_positions"},
        write_service=cast(OptionsWriteService, SimpleNamespace()),
        manage_service=cast(OptionsManageService, manage_service),
    )

    await run_option_management_stages(deps, {}, {}, options_enabled=True)

    manage_service.roll_puts.assert_awaited_once_with(["RP"], {})
    manage_service.roll_calls.assert_awaited_once_with(["RC"], {}, {})
    manage_service.close_puts.assert_not_called()
    manage_service.close_calls.assert_not_called()


@pytest.mark.asyncio
async def test_run_option_management_stages_close_only(mocker):
    manage_service = SimpleNamespace(
        check_puts=AsyncMock(return_value=(["RP"], ["CP"], "g1")),
        check_calls=AsyncMock(return_value=(["RC"], ["CC"], "g2")),
        roll_puts=AsyncMock(return_value=["RCP"]),
        roll_calls=AsyncMock(return_value=["RCC"]),
        close_puts=AsyncMock(),
        close_calls=AsyncMock(),
    )
    deps = OptionsStrategyDeps(
        enabled_stages={"options_close_positions"},
        write_service=cast(OptionsWriteService, SimpleNamespace()),
        manage_service=cast(OptionsManageService, manage_service),
    )

    await run_option_management_stages(deps, {}, {}, options_enabled=True)

    manage_service.roll_puts.assert_not_called()
    manage_service.roll_calls.assert_not_called()
    manage_service.close_puts.assert_awaited_once_with(["CP"])
    manage_service.close_calls.assert_awaited_once_with(["CC"])


@pytest.mark.asyncio
async def test_run_option_management_stages_roll_and_close_combines_results(mocker):
    manage_service = SimpleNamespace(
        check_puts=AsyncMock(return_value=(["RP"], ["CP"], "g1")),
        check_calls=AsyncMock(return_value=(["RC"], ["CC"], "g2")),
        roll_puts=AsyncMock(return_value=["RCP"]),
        roll_calls=AsyncMock(return_value=["RCC"]),
        close_puts=AsyncMock(),
        close_calls=AsyncMock(),
    )
    deps = OptionsStrategyDeps(
        enabled_stages={"options_roll_positions", "options_close_positions"},
        write_service=cast(OptionsWriteService, SimpleNamespace()),
        manage_service=cast(OptionsManageService, manage_service),
    )

    await run_option_management_stages(deps, {}, {}, options_enabled=True)

    manage_service.roll_puts.assert_awaited_once_with(["RP"], {})
    manage_service.roll_calls.assert_awaited_once_with(["RC"], {}, {})
    manage_service.close_puts.assert_awaited_once_with(["CP", "RCP"])
    manage_service.close_calls.assert_awaited_once_with(["CC", "RCC"])


@pytest.mark.asyncio
async def test_run_equity_rebalance_stages_regime_runs_even_if_table_hidden(mocker):
    regime_service = SimpleNamespace(
        check_regime_rebalance_positions=AsyncMock(return_value=("tbl", ["ORDER"])),
        execute_regime_rebalance_orders=AsyncMock(),
    )
    rebalance_service = SimpleNamespace(
        check_buy_only_positions=AsyncMock(return_value=("buy_tbl", [])),
        execute_buy_orders=AsyncMock(),
        check_sell_only_positions=AsyncMock(return_value=("sell_tbl", [])),
        execute_sell_orders=AsyncMock(),
    )
    deps = EquityStrategyDeps(
        enabled_stages={"equity_regime_rebalance"},
        regime_rebalance_enabled=False,
        regime_service=cast(RegimeRebalanceService, regime_service),
        rebalance_service=cast(EquityRebalanceService, rebalance_service),
    )
    print_mock = mocker.patch("thetagang.strategies.equity.log.print")

    await run_equity_rebalance_stages(deps, {}, {})

    print_mock.assert_not_called()
    regime_service.execute_regime_rebalance_orders.assert_awaited_once_with(["ORDER"])


@pytest.mark.asyncio
async def test_run_equity_rebalance_stages_buy_and_sell_paths(mocker):
    regime_service = SimpleNamespace(
        check_regime_rebalance_positions=AsyncMock(return_value=("tbl", [])),
        execute_regime_rebalance_orders=AsyncMock(),
    )
    rebalance_service = SimpleNamespace(
        check_buy_only_positions=AsyncMock(
            return_value=("buy_tbl", [("AAA", "NYSE", 2)])
        ),
        execute_buy_orders=AsyncMock(),
        check_sell_only_positions=AsyncMock(
            return_value=("sell_tbl", [("BBB", "NYSE", 1)])
        ),
        execute_sell_orders=AsyncMock(),
    )
    deps = EquityStrategyDeps(
        enabled_stages={"equity_buy_rebalance", "equity_sell_rebalance"},
        regime_rebalance_enabled=True,
        regime_service=cast(RegimeRebalanceService, regime_service),
        rebalance_service=cast(EquityRebalanceService, rebalance_service),
    )
    print_mock = mocker.patch("thetagang.strategies.equity.log.print")

    await run_equity_rebalance_stages(deps, {}, {})

    rebalance_service.execute_buy_orders.assert_awaited_once_with([("AAA", "NYSE", 2)])
    rebalance_service.execute_sell_orders.assert_awaited_once_with([("BBB", "NYSE", 1)])
    assert print_mock.call_count == 2


@pytest.mark.asyncio
async def test_run_post_stages_respects_enabled_stage_flags():
    service = SimpleNamespace(do_vix_hedging=AsyncMock(), do_cashman=AsyncMock())
    deps = PostStrategyDeps(
        enabled_stages={"post_cash_management"},
        service=cast(PostStageService, service),
    )

    await run_post_stages(deps, {}, {})

    service.do_vix_hedging.assert_not_called()
    service.do_cashman.assert_awaited_once_with({}, {})
