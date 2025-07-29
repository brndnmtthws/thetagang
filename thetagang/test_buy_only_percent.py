import pytest

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
class TestBuyOnlyPercentageThreshold:
    """Test cases for buy-only percentage threshold functionality."""

    async def test_buy_only_percentage_threshold_basic(self, portfolio_manager, mocker):
        """Test that buy-only rebalancing respects percentage threshold."""
        # Mock config with percentage threshold
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.1,
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=0.02,  # 2% of NLV
                buy_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # No existing positions
        portfolio_positions = {}

        # Mock get_buying_power
        portfolio_manager.get_buying_power = mocker.Mock(return_value=10000)

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

        # Target: 0.1 * 10000 = $1000, which is 6.66 shares (6 shares = $900)
        # 2% of $100,000 NLV = $2000 minimum
        # $900 < $2000, so should not buy
        assert len(to_buy) == 0

    async def test_buy_only_percentage_threshold_allows_purchase(
        self, portfolio_manager, mocker
    ):
        """Test that purchases are allowed when meeting percentage threshold."""
        # Mock config with small percentage threshold
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.2,  # 20% allocation
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=0.01,  # 1% of NLV
                buy_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # No existing positions
        portfolio_positions = {}

        # Mock get_buying_power
        portfolio_manager.get_buying_power = mocker.Mock(return_value=20000)

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

        # Target: 0.2 * 20000 = $4000, which is 26.66 shares (26 shares = $3900)
        # 1% of $100,000 NLV = $1000 minimum
        # $3900 > $1000, so should buy
        assert len(to_buy) == 1
        assert to_buy[0] == ("AAPL", "NASDAQ", 26)

    async def test_buy_only_percent_takes_precedence_over_amount(
        self, portfolio_manager, mocker
    ):
        """Test that percentage threshold takes precedence over dollar amount when larger."""
        # Mock config with both percentage and amount thresholds
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.15,
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=1000.0,  # $1000
                buy_only_min_threshold_percent=0.025,  # 2.5% of NLV
                buy_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # No existing positions
        portfolio_positions = {}

        # Mock get_buying_power
        portfolio_manager.get_buying_power = mocker.Mock(return_value=15000)

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

        # Target: 0.15 * 15000 = $2250, which is 15 shares = $2250
        # 2.5% of $100,000 NLV = $2500 minimum (larger than $1000 amount threshold)
        # $2250 < $2500, so should not buy
        assert len(to_buy) == 0

    async def test_buy_only_amount_takes_precedence_when_larger(
        self, portfolio_manager, mocker
    ):
        """Test that dollar amount threshold is used when it's larger than percentage."""
        # Mock config with both thresholds, amount being larger
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.15,
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=3000.0,  # $3000
                buy_only_min_threshold_percent=0.005,  # 0.5% of NLV = $500
                buy_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - NLV = $100,000
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # No existing positions
        portfolio_positions = {}

        # Mock get_buying_power
        portfolio_manager.get_buying_power = mocker.Mock(return_value=15000)

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

        # Target: 0.15 * 15000 = $2250, which is 15 shares = $2250
        # 0.5% of $100,000 = $500, but amount threshold is $3000 (larger)
        # $2250 < $3000, so should not buy
        assert len(to_buy) == 0
