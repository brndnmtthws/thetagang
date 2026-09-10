from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from ib_async import IB, AccountValue, OrderState

from thetagang.config import Config
from thetagang.config_models import AccountConfig, IBAsyncConfig, OrdersConfig
from thetagang.portfolio_manager import PortfolioManager


@pytest.fixture
def broker() -> Mock:
    ib = Mock(spec=IB)
    ib.orderStatusEvent = MagicMock()
    ib.client = Mock()
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    ib.reqExecutionsAsync = AsyncMock(return_value=[])
    ib.reqAccountUpdatesAsync = AsyncMock(return_value=None)
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    ib.portfolio.return_value = []
    ib.accountValues.return_value = [
        AccountValue("DU-TEST", tag, "10000", "USD", "")
        for tag in ("AvailableFunds", "ExcessLiquidity", "BuyingPower")
    ]
    ib.whatIfOrderAsync = AsyncMock(
        return_value=OrderState(
            status="PreSubmitted",
            initMarginChange="100",
            maintMarginChange="100",
            equityWithLoanChange="0",
        )
    )
    return ib


@pytest.fixture
def config() -> Config:
    return cast(
        Config,
        SimpleNamespace(
            runtime=SimpleNamespace(
                account=AccountConfig(number="DU-TEST", margin_usage=0.5),
                ib_async=IBAsyncConfig(api_response_wait_time=1),
                orders=OrdersConfig(price_update_delay=[1, 2]),
            ),
            portfolio=SimpleNamespace(
                symbols={"SPY": SimpleNamespace(adjust_price_after_delay=True)}
            ),
            strategies=SimpleNamespace(
                regime_rebalance=SimpleNamespace(enabled=False),
                vix_call_hedge=SimpleNamespace(enabled=True),
                cash_management=SimpleNamespace(enabled=True, cash_fund="SGOV"),
            ),
        ),
    )


@pytest.fixture
def manager(config: Config, broker: Mock) -> PortfolioManager:
    return PortfolioManager(config, broker, Mock(), dry_run=False, run_stage_order=[])
