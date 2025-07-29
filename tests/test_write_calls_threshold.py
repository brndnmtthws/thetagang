from typing import Dict
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from ib_async import AccountValue, Option, PortfolioItem, Stock, Ticker

from thetagang.portfolio_manager import PortfolioManager


@pytest.fixture
def mock_ib(mocker):
    """Fixture to create a mock IB object."""
    mock = mocker.Mock()
    mock.orderStatusEvent = mocker.Mock()
    mock.orderStatusEvent.__iadd__ = mocker.Mock(return_value=None)
    return mock


@pytest.fixture
def mock_config(mocker):
    """Fixture to create a mock Config object."""
    config = mocker.Mock()
    config.account = mocker.Mock()
    config.account.number = "TEST123"
    config.account.margin_usage = 1.0
    config.orders = mocker.Mock()
    config.orders.exchange = "SMART"
    config.trading_is_allowed = mocker.Mock(return_value=True)
    config.write_excess_calls_only = mocker.Mock(return_value=False)
    config.get_cap_factor = mocker.Mock(return_value=1.0)
    config.get_cap_target_floor = mocker.Mock(return_value=0.0)
    config.write_when = mocker.Mock()
    config.write_when.calculate_net_contracts = False
    config.write_when.calls = mocker.Mock()
    config.write_when.calls.min_threshold_percent = None
    config.write_when.calls.min_threshold_percent_relative = None
    config.can_write_when = mocker.Mock(return_value=(True, True))
    config.get_symbols = mocker.Mock(return_value=[])
    config.get_strike_limit = mocker.Mock(return_value=None)
    return config


@pytest.fixture
def portfolio_manager(mock_ib, mock_config, mocker):
    """Fixture to create a PortfolioManager instance."""
    completion_future = mocker.Mock()
    pm = PortfolioManager(mock_config, mock_ib, completion_future, dry_run=False)
    pm.target_quantities = {}
    return pm


def create_account_summary(net_liquidation: float) -> Dict[str, AccountValue]:
    """Create mock account summary."""
    return {
        "NetLiquidation": AccountValue(
            account="",
            tag="NetLiquidation",
            value=str(net_liquidation),
            currency="",
            modelCode="",
        ),
    }


def create_ticker(symbol: str, market_price: float) -> Ticker:
    """Create a mock ticker."""
    ticker = MagicMock(spec=Ticker)
    ticker.marketPrice.return_value = market_price
    ticker.contract = Stock(symbol, "SMART", "USD")
    return ticker


def create_stock_position(symbol: str, position: int, avg_cost: float) -> PortfolioItem:
    """Create a mock stock position."""
    return PortfolioItem(
        account="",
        contract=Stock(symbol, "SMART", "USD"),
        position=position,
        marketPrice=0.0,
        marketValue=0.0,
        averageCost=avg_cost,
        unrealizedPNL=0.0,
        realizedPNL=0.0,
    )


def create_call_position(symbol: str, position: int, strike: float) -> PortfolioItem:
    """Create a mock call option position."""
    return PortfolioItem(
        account="",
        contract=Option(
            symbol=symbol,
            lastTradeDateOrContractMonth="20250220",
            strike=strike,
            right="C",
            exchange="SMART",
            currency="USD",
        ),
        position=position,
        marketPrice=0.0,
        marketValue=0.0,
        averageCost=0.0,
        unrealizedPNL=0.0,
        realizedPNL=0.0,
    )


@pytest.mark.asyncio
async def test_write_calls_absolute_threshold_blocks(portfolio_manager, mocker):
    """Test that absolute threshold blocks call writing when position is too small."""
    # Configure for 2% allocation with 5% threshold
    portfolio_manager.config.symbols = {
        "SPY": mocker.Mock(
            weight=0.02,  # 2% allocation
            write_calls_only_min_threshold_percent=0.05,  # 5% threshold
            write_calls_only_min_threshold_percent_relative=None,
            primary_exchange="SMART",
        )
    }
    portfolio_manager.config.get_symbols = mocker.Mock(return_value=["SPY"])
    portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=True)
    portfolio_manager.config.can_write_when = mocker.Mock(return_value=(True, True))
    portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(return_value=False)
    portfolio_manager.config.write_when = mocker.Mock()
    portfolio_manager.config.write_when.calls = mocker.Mock()
    portfolio_manager.config.write_when.calls.min_threshold_percent = None
    portfolio_manager.config.write_when.calls.min_threshold_percent_relative = None

    account_summary = create_account_summary(100000)  # $100k NLV

    # SPY position worth $3k (3% of NLV, below 5% threshold)
    portfolio_positions = {
        "SPY": [create_stock_position("SPY", 100, 300.0)],
    }

    # Set up mocks
    ticker = create_ticker("SPY", 30.0)  # $30 per share
    portfolio_manager.ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    portfolio_manager.get_maximum_new_contracts_for = AsyncMock(return_value=1)
    portfolio_manager.get_close_price = Mock(return_value=29.0)
    portfolio_manager.get_write_threshold = AsyncMock(
        return_value=(0.01, 0.05)
    )  # Passes daily change check
    portfolio_manager.target_quantities["SPY"] = 67  # Target is ~$2k at $30
    portfolio_manager.get_primary_exchange = Mock(return_value="SMART")

    # Execute
    _, to_write = await portfolio_manager.check_for_uncovered_positions(
        account_summary, portfolio_positions
    )

    # Should not write calls because position is only 3% of NLV (below 5% threshold)
    assert len(to_write) == 0


@pytest.mark.asyncio
async def test_write_calls_absolute_threshold_allows(portfolio_manager, mocker):
    """Test that absolute threshold allows call writing when position is large enough."""
    # Configure for 6% allocation with 5% threshold
    portfolio_manager.config.symbols = {
        "SPY": mocker.Mock(
            weight=0.06,  # 6% allocation
            write_calls_only_min_threshold_percent=0.05,  # 5% threshold
            write_calls_only_min_threshold_percent_relative=None,
            primary_exchange="SMART",
        )
    }
    portfolio_manager.config.get_symbols = mocker.Mock(return_value=["SPY"])
    portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=True)
    portfolio_manager.config.can_write_when = mocker.Mock(return_value=(True, True))
    portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(return_value=False)
    portfolio_manager.config.write_when = mocker.Mock()
    portfolio_manager.config.write_when.calls = mocker.Mock()
    portfolio_manager.config.write_when.calls.min_threshold_percent = None
    portfolio_manager.config.write_when.calls.min_threshold_percent_relative = None

    account_summary = create_account_summary(100000)  # $100k NLV

    # SPY position worth $6k (6% of NLV, above 5% threshold)
    portfolio_positions = {
        "SPY": [create_stock_position("SPY", 200, 300.0)],
    }

    # Set up mocks
    ticker = create_ticker("SPY", 30.0)  # $30 per share
    portfolio_manager.ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    portfolio_manager.get_maximum_new_contracts_for = AsyncMock(return_value=2)
    portfolio_manager.get_close_price = Mock(return_value=29.0)
    portfolio_manager.get_write_threshold = AsyncMock(
        return_value=(0.01, 0.05)
    )  # Passes daily change check
    portfolio_manager.target_quantities["SPY"] = 200  # Target matches current position
    portfolio_manager.get_primary_exchange = Mock(return_value="SMART")

    # Execute
    _, to_write = await portfolio_manager.check_for_uncovered_positions(
        account_summary, portfolio_positions
    )

    # Should write calls because position is 6% of NLV (above 5% threshold)
    assert len(to_write) == 1
    assert to_write[0][0] == "SPY"
    assert to_write[0][2] == 2  # Write 2 calls for 200 shares


@pytest.mark.asyncio
async def test_write_calls_relative_threshold_blocks(portfolio_manager, mocker):
    """Test that relative threshold blocks call writing when position is not sufficiently above target."""
    # Configure for 10% target with 20% relative threshold
    portfolio_manager.config.symbols = {
        "SPY": mocker.Mock(
            weight=0.10,  # 10% target allocation
            write_calls_only_min_threshold_percent=None,
            write_calls_only_min_threshold_percent_relative=0.2,  # 20% above target
            primary_exchange="SMART",
        )
    }
    portfolio_manager.config.get_symbols = mocker.Mock(return_value=["SPY"])
    portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=True)
    portfolio_manager.config.can_write_when = mocker.Mock(return_value=(True, True))
    portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(return_value=False)
    portfolio_manager.config.write_when = mocker.Mock()
    portfolio_manager.config.write_when.calls = mocker.Mock()
    portfolio_manager.config.write_when.calls.min_threshold_percent = None
    portfolio_manager.config.write_when.calls.min_threshold_percent_relative = None

    account_summary = create_account_summary(100000)  # $100k NLV

    # SPY position worth $11k (10% above target of $10k, below 20% threshold)
    portfolio_positions = {
        "SPY": [create_stock_position("SPY", 1100, 10.0)],
    }

    # Set up mocks
    ticker = create_ticker("SPY", 10.0)  # $10 per share
    portfolio_manager.ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    portfolio_manager.get_maximum_new_contracts_for = AsyncMock(return_value=11)
    portfolio_manager.get_close_price = Mock(return_value=9.8)
    portfolio_manager.get_write_threshold = AsyncMock(
        return_value=(0.01, 0.05)
    )  # Passes daily change check
    portfolio_manager.target_quantities["SPY"] = 1000  # Target is 1000 shares ($10k)
    portfolio_manager.get_primary_exchange = Mock(return_value="SMART")

    # Execute
    _, to_write = await portfolio_manager.check_for_uncovered_positions(
        account_summary, portfolio_positions
    )

    # Should not write calls because position is only 10% above target (below 20% threshold)
    assert len(to_write) == 0


@pytest.mark.asyncio
async def test_write_calls_relative_threshold_allows(portfolio_manager, mocker):
    """Test that relative threshold allows call writing when position is sufficiently above target."""
    # Configure for 10% target with 20% relative threshold
    portfolio_manager.config.symbols = {
        "SPY": mocker.Mock(
            weight=0.10,  # 10% target allocation
            write_calls_only_min_threshold_percent=None,
            write_calls_only_min_threshold_percent_relative=0.2,  # 20% above target
            primary_exchange="SMART",
        )
    }
    portfolio_manager.config.get_symbols = mocker.Mock(return_value=["SPY"])
    portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=True)
    portfolio_manager.config.can_write_when = mocker.Mock(return_value=(True, True))
    portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(return_value=False)
    portfolio_manager.config.write_when = mocker.Mock()
    portfolio_manager.config.write_when.calls = mocker.Mock()
    portfolio_manager.config.write_when.calls.min_threshold_percent = None
    portfolio_manager.config.write_when.calls.min_threshold_percent_relative = None

    account_summary = create_account_summary(100000)  # $100k NLV

    # SPY position worth $13k (30% above target of $10k, above 20% threshold)
    portfolio_positions = {
        "SPY": [create_stock_position("SPY", 1300, 10.0)],
    }

    # Set up mocks
    ticker = create_ticker("SPY", 10.0)  # $10 per share
    portfolio_manager.ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    portfolio_manager.get_maximum_new_contracts_for = AsyncMock(return_value=13)
    portfolio_manager.get_close_price = Mock(return_value=9.8)
    portfolio_manager.get_write_threshold = AsyncMock(
        return_value=(0.01, 0.05)
    )  # Passes daily change check
    portfolio_manager.target_quantities["SPY"] = 1000  # Target is 1000 shares ($10k)
    portfolio_manager.get_primary_exchange = Mock(return_value="SMART")

    # Execute
    _, to_write = await portfolio_manager.check_for_uncovered_positions(
        account_summary, portfolio_positions
    )

    # Should write calls because position is 30% above target (above 20% threshold)
    assert len(to_write) == 1
    assert to_write[0][0] == "SPY"
    assert to_write[0][2] == 13  # Write 13 calls for 1300 shares


@pytest.mark.asyncio
async def test_write_calls_both_thresholds(portfolio_manager, mocker):
    """Test that both thresholds must be satisfied when both are configured."""
    # Configure with both thresholds
    portfolio_manager.config.symbols = {
        "SPY": mocker.Mock(
            weight=0.04,  # 4% target allocation
            write_calls_only_min_threshold_percent=0.05,  # 5% of NLV threshold
            write_calls_only_min_threshold_percent_relative=0.1,  # 10% above target
            primary_exchange="SMART",
        )
    }
    portfolio_manager.config.get_symbols = mocker.Mock(return_value=["SPY"])
    portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=True)
    portfolio_manager.config.can_write_when = mocker.Mock(return_value=(True, True))
    portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(return_value=False)
    portfolio_manager.config.write_when = mocker.Mock()
    portfolio_manager.config.write_when.calls = mocker.Mock()
    portfolio_manager.config.write_when.calls.min_threshold_percent = None
    portfolio_manager.config.write_when.calls.min_threshold_percent_relative = None

    account_summary = create_account_summary(100000)  # $100k NLV

    # SPY position worth $4.5k (4.5% of NLV, 12.5% above target)
    # Fails absolute threshold (4.5% < 5%) but passes relative threshold (12.5% > 10%)
    portfolio_positions = {
        "SPY": [create_stock_position("SPY", 450, 10.0)],
    }

    # Set up mocks
    ticker = create_ticker("SPY", 10.0)  # $10 per share
    portfolio_manager.ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    portfolio_manager.get_maximum_new_contracts_for = AsyncMock(return_value=4)
    portfolio_manager.get_close_price = Mock(return_value=9.8)
    portfolio_manager.get_write_threshold = AsyncMock(
        return_value=(0.01, 0.05)
    )  # Passes daily change check
    portfolio_manager.target_quantities["SPY"] = 400  # Target is 400 shares ($4k)
    portfolio_manager.get_primary_exchange = Mock(return_value="SMART")

    # Execute
    _, to_write = await portfolio_manager.check_for_uncovered_positions(
        account_summary, portfolio_positions
    )

    # Should not write calls because absolute threshold is not met
    assert len(to_write) == 0


@pytest.mark.asyncio
async def test_write_calls_no_thresholds(portfolio_manager, mocker):
    """Test that calls are written normally when no thresholds are configured."""
    # Configure with no thresholds
    portfolio_manager.config.symbols = {
        "SPY": mocker.Mock(
            weight=0.01,  # 1% allocation (small position)
            write_calls_only_min_threshold_percent=None,
            write_calls_only_min_threshold_percent_relative=None,
            primary_exchange="SMART",
        )
    }
    portfolio_manager.config.get_symbols = mocker.Mock(return_value=["SPY"])
    portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=True)
    portfolio_manager.config.can_write_when = mocker.Mock(return_value=(True, True))
    portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(return_value=False)
    portfolio_manager.config.write_when = mocker.Mock()
    portfolio_manager.config.write_when.calls = mocker.Mock()
    portfolio_manager.config.write_when.calls.min_threshold_percent = None
    portfolio_manager.config.write_when.calls.min_threshold_percent_relative = None

    account_summary = create_account_summary(100000)  # $100k NLV

    # SPY position worth $1k (1% of NLV)
    portfolio_positions = {
        "SPY": [create_stock_position("SPY", 100, 10.0)],
    }

    # Set up mocks
    ticker = create_ticker("SPY", 10.0)  # $10 per share
    portfolio_manager.ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    portfolio_manager.get_maximum_new_contracts_for = AsyncMock(return_value=1)
    portfolio_manager.get_close_price = Mock(return_value=9.8)
    portfolio_manager.get_write_threshold = AsyncMock(
        return_value=(0.01, 0.05)
    )  # Passes daily change check
    portfolio_manager.target_quantities["SPY"] = 100  # Target matches current
    portfolio_manager.get_primary_exchange = Mock(return_value="SMART")

    # Execute
    _, to_write = await portfolio_manager.check_for_uncovered_positions(
        account_summary, portfolio_positions
    )

    # Should write calls because no thresholds are configured
    assert len(to_write) == 1
    assert to_write[0][0] == "SPY"
    assert to_write[0][2] == 1  # Write 1 call for 100 shares


@pytest.mark.asyncio
async def test_write_calls_global_defaults(portfolio_manager, mocker):
    """Test that global defaults are used when symbol-specific values are not set."""
    # Configure with global defaults but no symbol-specific values
    portfolio_manager.config.write_when = mocker.Mock()
    portfolio_manager.config.write_when.calls = mocker.Mock()
    portfolio_manager.config.write_when.calls.min_threshold_percent = (
        0.05  # Global 5% threshold
    )
    portfolio_manager.config.write_when.calls.min_threshold_percent_relative = (
        0.2  # Global 20% relative
    )

    portfolio_manager.config.symbols = {
        "SPY": mocker.Mock(
            weight=0.04,  # 4% allocation
            write_calls_only_min_threshold_percent=None,  # No symbol-specific value
            write_calls_only_min_threshold_percent_relative=None,  # No symbol-specific value
            primary_exchange="SMART",
        )
    }
    portfolio_manager.config.get_symbols = mocker.Mock(return_value=["SPY"])
    portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=True)
    portfolio_manager.config.can_write_when = mocker.Mock(return_value=(True, True))
    portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(return_value=False)

    account_summary = create_account_summary(100000)  # $100k NLV

    # SPY position worth $4k (4% of NLV, below global 5% threshold)
    portfolio_positions = {
        "SPY": [create_stock_position("SPY", 400, 10.0)],
    }

    # Set up mocks
    ticker = create_ticker("SPY", 10.0)  # $10 per share
    portfolio_manager.ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    portfolio_manager.get_maximum_new_contracts_for = AsyncMock(return_value=4)
    portfolio_manager.get_close_price = Mock(return_value=9.8)
    portfolio_manager.get_write_threshold = AsyncMock(return_value=(0.01, 0.05))
    portfolio_manager.target_quantities["SPY"] = 400  # Target matches current
    portfolio_manager.get_primary_exchange = Mock(return_value="SMART")

    # Execute
    _, to_write = await portfolio_manager.check_for_uncovered_positions(
        account_summary, portfolio_positions
    )

    # Should not write calls because position is 4% (below global 5% threshold)
    assert len(to_write) == 0


@pytest.mark.asyncio
async def test_write_calls_symbol_overrides_global(portfolio_manager, mocker):
    """Test that symbol-specific values override global defaults."""
    # Configure with global defaults that would block, but symbol-specific that allows
    portfolio_manager.config.write_when = mocker.Mock()
    portfolio_manager.config.write_when.calls = mocker.Mock()
    portfolio_manager.config.write_when.calls.min_threshold_percent = (
        0.10  # Global 10% threshold
    )
    portfolio_manager.config.write_when.calls.min_threshold_percent_relative = None

    portfolio_manager.config.symbols = {
        "SPY": mocker.Mock(
            weight=0.06,  # 6% allocation
            write_calls_only_min_threshold_percent=0.05,  # Symbol-specific 5% (overrides global)
            write_calls_only_min_threshold_percent_relative=None,
            primary_exchange="SMART",
        )
    }
    portfolio_manager.config.get_symbols = mocker.Mock(return_value=["SPY"])
    portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=True)
    portfolio_manager.config.can_write_when = mocker.Mock(return_value=(True, True))
    portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(return_value=False)

    account_summary = create_account_summary(100000)  # $100k NLV

    # SPY position worth $6k (6% of NLV, above symbol-specific 5% but below global 10%)
    portfolio_positions = {
        "SPY": [create_stock_position("SPY", 600, 10.0)],
    }

    # Set up mocks
    ticker = create_ticker("SPY", 10.0)  # $10 per share
    portfolio_manager.ibkr.get_ticker_for_stock = AsyncMock(return_value=ticker)
    portfolio_manager.get_maximum_new_contracts_for = AsyncMock(return_value=6)
    portfolio_manager.get_close_price = Mock(return_value=9.8)
    portfolio_manager.get_write_threshold = AsyncMock(return_value=(0.01, 0.05))
    portfolio_manager.target_quantities["SPY"] = 600  # Target matches current
    portfolio_manager.get_primary_exchange = Mock(return_value="SMART")

    # Execute
    _, to_write = await portfolio_manager.check_for_uncovered_positions(
        account_summary, portfolio_positions
    )

    # Should write calls because symbol-specific 5% overrides global 10%
    assert len(to_write) == 1
    assert to_write[0][0] == "SPY"
    assert to_write[0][2] == 6
