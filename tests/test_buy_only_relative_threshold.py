import pytest
from ib_async import Stock

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
    config.ib_async = mocker.Mock()
    config.ib_async.api_response_wait_time = 1
    config.orders = mocker.Mock()
    config.orders.exchange = "SMART"
    return config


@pytest.fixture
def portfolio_manager(mock_ib, mock_config, mocker):
    """Fixture to create a PortfolioManager instance."""
    completion_future = mocker.Mock()
    return PortfolioManager(mock_config, mock_ib, completion_future, dry_run=False)


@pytest.mark.asyncio
class TestBuyOnlyRelativeThreshold:
    """Test cases for buy-only relative percentage threshold functionality."""

    async def test_relative_threshold_blocks_small_differences(
        self, portfolio_manager, mocker
    ):
        """Test that relative threshold blocks purchases when difference is too small."""
        # Mock config with relative threshold
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.5,  # 50% allocation
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=0.2,  # 20% relative threshold
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # Existing position: 250 shares at $150 = $37,500 (75% of target)
        mock_stock_contract = mocker.MagicMock(spec=Stock)
        mock_stock_contract.symbol = "AAPL"
        portfolio_positions = {
            "AAPL": [mocker.Mock(contract=mock_stock_contract, position=250)]
        }

        # Mock get_buying_power
        portfolio_manager.get_buying_power = mocker.Mock(return_value=50000)

        # Mock IBKR methods
        mock_ticker = mocker.Mock()
        mock_ticker.marketPrice.return_value = 150.0  # $150 per share
        portfolio_manager.ibkr.get_ticker_for_stock = mocker.AsyncMock(
            return_value=mock_ticker
        )

        # Mock get_primary_exchange
        portfolio_manager.get_primary_exchange = mocker.Mock(return_value="NASDAQ")

        # Mock log.track_async
        async def mock_track_async(tasks, description):
            for task in tasks:
                await task

        mocker.patch("thetagang.log.track_async", side_effect=mock_track_async)

        # Call the method
        buy_actions_table, to_buy = await portfolio_manager.check_buy_only_positions(
            account_summary, portfolio_positions
        )

        # Target: 0.5 * 50000 = $25,000, which is 166.66 shares (166 shares)
        # Current: 250 shares * $150 = $37,500
        # We're above target, so shares_to_buy would be negative
        # No purchase should be made
        assert len(to_buy) == 0

    async def test_relative_threshold_allows_large_differences(
        self, portfolio_manager, mocker
    ):
        """Test that purchases are allowed when relative difference exceeds threshold."""
        # Mock config with relative threshold
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.5,  # 50% allocation
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=0.1,  # 10% relative threshold
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # No existing position
        portfolio_positions = {}

        # Mock get_buying_power
        portfolio_manager.get_buying_power = mocker.Mock(return_value=50000)

        # Mock IBKR methods
        mock_ticker = mocker.Mock()
        mock_ticker.marketPrice.return_value = 150.0  # $150 per share
        portfolio_manager.ibkr.get_ticker_for_stock = mocker.AsyncMock(
            return_value=mock_ticker
        )

        # Mock get_primary_exchange
        portfolio_manager.get_primary_exchange = mocker.Mock(return_value="NASDAQ")

        # Mock log.track_async
        async def mock_track_async(tasks, description):
            for task in tasks:
                await task

        mocker.patch("thetagang.log.track_async", side_effect=mock_track_async)

        # Call the method
        buy_actions_table, to_buy = await portfolio_manager.check_buy_only_positions(
            account_summary, portfolio_positions
        )

        # Target: 0.5 * 50000 = $25,000, which is 166.66 shares (166 shares)
        # Current: 0 shares
        # Relative difference: (25000 - 0) / 25000 = 100%
        # 100% > 10% threshold, so should buy
        assert len(to_buy) == 1
        assert to_buy[0] == ("AAPL", "NASDAQ", 166)

    async def test_relative_threshold_with_partial_position(
        self, portfolio_manager, mocker
    ):
        """Test relative threshold with a partial position."""
        # Mock config with relative threshold
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.4,  # 40% allocation
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=0.3,  # 30% relative threshold
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # Existing position: 50 shares at $200 = $10,000 (50% of target)
        mock_stock_contract = mocker.MagicMock(spec=Stock)
        mock_stock_contract.symbol = "AAPL"
        portfolio_positions = {
            "AAPL": [mocker.Mock(contract=mock_stock_contract, position=50)]
        }

        # Mock get_buying_power
        portfolio_manager.get_buying_power = mocker.Mock(return_value=40000)

        # Mock IBKR methods
        mock_ticker = mocker.Mock()
        mock_ticker.marketPrice.return_value = 200.0  # $200 per share
        portfolio_manager.ibkr.get_ticker_for_stock = mocker.AsyncMock(
            return_value=mock_ticker
        )

        # Mock get_primary_exchange
        portfolio_manager.get_primary_exchange = mocker.Mock(return_value="NASDAQ")

        # Mock log.track_async
        async def mock_track_async(tasks, description):
            for task in tasks:
                await task

        mocker.patch("thetagang.log.track_async", side_effect=mock_track_async)

        # Call the method
        buy_actions_table, to_buy = await portfolio_manager.check_buy_only_positions(
            account_summary, portfolio_positions
        )

        # Target: 0.4 * 40000 = $16,000, which is 80 shares
        # Current: 50 shares * $200 = $10,000
        # Shares to buy: 80 - 50 = 30
        # Relative difference: (16000 - 10000) / 16000 = 37.5%
        # 37.5% > 30% threshold, so should buy
        assert len(to_buy) == 1
        assert to_buy[0] == ("AAPL", "NASDAQ", 30)

    async def test_relative_threshold_priority_over_absolute(
        self, portfolio_manager, mocker
    ):
        """Test that relative threshold takes priority over absolute threshold."""
        # Mock config with both relative and absolute thresholds
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.2,  # 20% allocation
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=0.001,  # 0.1% of NLV (very low)
                buy_only_min_threshold_percent_relative=0.5,  # 50% relative threshold
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # Existing position: 40 shares at $100 = $4,000 (40% of target)
        mock_stock_contract = mocker.MagicMock(spec=Stock)
        mock_stock_contract.symbol = "AAPL"
        portfolio_positions = {
            "AAPL": [mocker.Mock(contract=mock_stock_contract, position=40)]
        }

        # Mock get_buying_power
        portfolio_manager.get_buying_power = mocker.Mock(return_value=20000)

        # Mock IBKR methods
        mock_ticker = mocker.Mock()
        mock_ticker.marketPrice.return_value = 100.0  # $100 per share
        portfolio_manager.ibkr.get_ticker_for_stock = mocker.AsyncMock(
            return_value=mock_ticker
        )

        # Mock get_primary_exchange
        portfolio_manager.get_primary_exchange = mocker.Mock(return_value="NASDAQ")

        # Mock log.track_async
        async def mock_track_async(tasks, description):
            for task in tasks:
                await task

        mocker.patch("thetagang.log.track_async", side_effect=mock_track_async)

        # Call the method
        buy_actions_table, to_buy = await portfolio_manager.check_buy_only_positions(
            account_summary, portfolio_positions
        )

        # Target: 0.2 * 20000 = $4,000, which is 40 shares
        # Current: 40 shares * $100 = $4,000
        # We're exactly at target, so shares_to_buy = 0
        # Even though we meet the absolute threshold, relative difference is 0%
        # 0% < 50% relative threshold, so should NOT buy
        assert len(to_buy) == 0

    async def test_relative_threshold_edge_case_zero_target(
        self, portfolio_manager, mocker
    ):
        """Test that relative threshold is ignored when target value is zero."""
        # Mock config with relative threshold
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.0,  # 0% allocation
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=0.1,  # 10% relative threshold
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # No existing position
        portfolio_positions = {}

        # Mock get_buying_power
        portfolio_manager.get_buying_power = mocker.Mock(return_value=50000)

        # Mock IBKR methods
        mock_ticker = mocker.Mock()
        mock_ticker.marketPrice.return_value = 150.0  # $150 per share
        portfolio_manager.ibkr.get_ticker_for_stock = mocker.AsyncMock(
            return_value=mock_ticker
        )

        # Mock get_primary_exchange
        portfolio_manager.get_primary_exchange = mocker.Mock(return_value="NASDAQ")

        # Mock log.track_async
        async def mock_track_async(tasks, description):
            for task in tasks:
                await task

        mocker.patch("thetagang.log.track_async", side_effect=mock_track_async)

        # Call the method
        buy_actions_table, to_buy = await portfolio_manager.check_buy_only_positions(
            account_summary, portfolio_positions
        )

        # Target: 0.0 * 50000 = $0
        # With 0% allocation, no purchase should be made regardless of threshold
        assert len(to_buy) == 0
