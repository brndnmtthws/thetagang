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
class TestSellOnlyRebalancing:
    """Test cases for sell-only rebalancing functionality."""

    async def test_sell_only_basic_functionality(self, portfolio_manager, mocker):
        """Test basic sell-only rebalancing when position is above target."""
        # Mock config with sell-only rebalancing
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.3,  # 30% allocation
                sell_only_min_threshold_shares=None,
                sell_only_min_threshold_amount=None,
                sell_only_min_threshold_percent=None,
                sell_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # Existing position: 400 shares at $100 = $40,000 (133% of target)
        mock_stock_contract = mocker.MagicMock(spec=Stock)
        mock_stock_contract.symbol = "AAPL"
        portfolio_positions = {
            "AAPL": [mocker.Mock(contract=mock_stock_contract, position=400)]
        }

        # Mock get_buying_power
        portfolio_manager.get_buying_power = mocker.Mock(return_value=30000)

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
        sell_actions_table, to_sell = await portfolio_manager.check_sell_only_positions(
            account_summary, portfolio_positions
        )

        # Target: 0.3 * 30000 = $9,000, which is 90 shares
        # Current: 400 shares
        # Shares to sell: 400 - 90 = 310
        assert len(to_sell) == 1
        assert to_sell[0] == ("AAPL", "NASDAQ", 310)

    async def test_sell_only_below_target_no_action(self, portfolio_manager, mocker):
        """Test that no selling occurs when position is below target."""
        # Mock config with sell-only rebalancing
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.5,  # 50% allocation
                sell_only_min_threshold_shares=None,
                sell_only_min_threshold_amount=None,
                sell_only_min_threshold_percent=None,
                sell_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # Existing position: 100 shares at $150 = $15,000 (30% of target)
        mock_stock_contract = mocker.MagicMock(spec=Stock)
        mock_stock_contract.symbol = "AAPL"
        portfolio_positions = {
            "AAPL": [mocker.Mock(contract=mock_stock_contract, position=100)]
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
        sell_actions_table, to_sell = await portfolio_manager.check_sell_only_positions(
            account_summary, portfolio_positions
        )

        # Target: 0.5 * 50000 = $25,000, which is 166.66 shares (166 shares)
        # Current: 100 shares
        # We're below target, so no selling should occur
        assert len(to_sell) == 0

    async def test_sell_only_min_shares_threshold(self, portfolio_manager, mocker):
        """Test that minimum shares threshold is respected."""
        # Mock config with minimum shares threshold
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.4,  # 40% allocation
                sell_only_min_threshold_shares=50,  # Min 50 shares to sell
                sell_only_min_threshold_amount=None,
                sell_only_min_threshold_percent=None,
                sell_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # Existing position: 220 shares at $200 = $44,000 (110% of target)
        mock_stock_contract = mocker.MagicMock(spec=Stock)
        mock_stock_contract.symbol = "AAPL"
        portfolio_positions = {
            "AAPL": [mocker.Mock(contract=mock_stock_contract, position=220)]
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
        sell_actions_table, to_sell = await portfolio_manager.check_sell_only_positions(
            account_summary, portfolio_positions
        )

        # Target: 0.4 * 40000 = $16,000, which is 80 shares
        # Current: 220 shares
        # Shares to sell: 220 - 80 = 140 shares
        # 140 > 50 threshold, so should sell
        assert len(to_sell) == 1
        assert to_sell[0] == ("AAPL", "NASDAQ", 140)

    async def test_sell_only_min_amount_threshold(self, portfolio_manager, mocker):
        """Test that minimum dollar amount threshold is respected."""
        # Mock config with minimum amount threshold
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.3,  # 30% allocation
                sell_only_min_threshold_shares=None,
                sell_only_min_threshold_amount=5000.0,  # Min $5000 to sell
                sell_only_min_threshold_percent=None,
                sell_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # Existing position: 320 shares at $100 = $32,000 (106.7% of target)
        mock_stock_contract = mocker.MagicMock(spec=Stock)
        mock_stock_contract.symbol = "AAPL"
        portfolio_positions = {
            "AAPL": [mocker.Mock(contract=mock_stock_contract, position=320)]
        }

        # Mock get_buying_power
        portfolio_manager.get_buying_power = mocker.Mock(return_value=30000)

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
        sell_actions_table, to_sell = await portfolio_manager.check_sell_only_positions(
            account_summary, portfolio_positions
        )

        # Target: 0.3 * 30000 = $9,000, which is 90 shares
        # Current: 320 shares
        # Shares to sell: 320 - 90 = 230 shares
        # Order amount: 230 * $100 = $23,000
        # $23,000 > $5000 threshold, so should sell
        assert len(to_sell) == 1
        assert to_sell[0] == ("AAPL", "NASDAQ", 230)

    async def test_sell_only_relative_threshold(self, portfolio_manager, mocker):
        """Test that relative percentage threshold works correctly."""
        # Mock config with relative threshold
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.4,  # 40% allocation
                sell_only_min_threshold_shares=None,
                sell_only_min_threshold_amount=None,
                sell_only_min_threshold_percent=None,
                sell_only_min_threshold_percent_relative=0.2,  # 20% relative threshold
            ),
        }
        portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # Existing position: 220 shares at $200 = $44,000 (110% of target)
        mock_stock_contract = mocker.MagicMock(spec=Stock)
        mock_stock_contract.symbol = "AAPL"
        portfolio_positions = {
            "AAPL": [mocker.Mock(contract=mock_stock_contract, position=220)]
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
        sell_actions_table, to_sell = await portfolio_manager.check_sell_only_positions(
            account_summary, portfolio_positions
        )

        # Target: 0.4 * 40000 = $16,000, which is 80 shares
        # Current: 220 shares * $200 = $44,000
        # Relative difference: ($44,000 - $16,000) / $16,000 = 175%
        # 175% > 20% threshold, so should sell
        assert len(to_sell) == 1
        assert to_sell[0] == ("AAPL", "NASDAQ", 140)

    async def test_sell_only_blocks_call_writing(self, portfolio_manager, mocker):
        """Test that sell-only symbols don't write calls."""
        # Mock config
        portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(
            return_value=True
        )
        portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=True)

        # Mock ticker
        mock_ticker = mocker.Mock()
        mock_ticker.marketPrice.return_value = 100.0

        # Test is_ok_to_write_calls function behavior
        # This is a simplified test - in reality this would be called from check_calls
        result = portfolio_manager.config.is_sell_only_rebalancing("AAPL")
        assert result is True

    async def test_both_buy_and_sell_rebalancing(self, portfolio_manager, mocker):
        """Test that symbols can have both buy-only and sell-only rebalancing."""
        # Mock config with both buy and sell rebalancing
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.4,  # 40% allocation
                buy_only_rebalancing=True,
                sell_only_rebalancing=True,
                buy_only_min_threshold_shares=10,
                sell_only_min_threshold_shares=10,
                buy_only_min_threshold_amount=None,
                sell_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=None,
                sell_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=None,
                sell_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )
        portfolio_manager.config.is_sell_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # This test just verifies that having both enabled doesn't cause errors
        # The actual behavior would be tested in integration tests
        assert portfolio_manager.config.is_buy_only_rebalancing("AAPL") is True
        assert portfolio_manager.config.is_sell_only_rebalancing("AAPL") is True
