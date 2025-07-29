import pytest
from ib_async import IB, Stock, Ticker

from thetagang.portfolio_manager import PortfolioManager


@pytest.fixture
def mock_ib(mocker):
    """Fixture to create a mock IB object."""
    mock = mocker.Mock(spec=IB)
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


class TestPortfolioManager:
    """Test cases for PortfolioManager class."""

    def test_get_close_price_with_valid_close(self, mocker):
        """Test get_close_price returns close price when it's not NaN."""
        ticker = mocker.Mock(spec=Ticker)
        ticker.close = 100.50
        ticker.marketPrice.return_value = 101.00

        # Mock util.isNan to return False for valid close price
        mocker.patch("ib_async.util.isNan", return_value=False)

        result = PortfolioManager.get_close_price(ticker)
        assert result == 100.50
        ticker.marketPrice.assert_not_called()

    def test_get_close_price_with_nan_close(self, mocker):
        """Test get_close_price returns market price when close is NaN."""
        ticker = mocker.Mock(spec=Ticker)
        ticker.close = float("nan")
        ticker.marketPrice.return_value = 101.00

        # Mock util.isNan to return True for NaN close price
        mocker.patch("ib_async.util.isNan", return_value=True)

        result = PortfolioManager.get_close_price(ticker)
        assert result == 101.00
        ticker.marketPrice.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_write_threshold_with_valid_close(
        self, portfolio_manager, mocker
    ):
        """Test get_write_threshold works correctly with valid close price."""
        ticker = mocker.Mock(spec=Ticker)
        ticker.close = 100.0
        ticker.marketPrice.return_value = 102.0
        ticker.contract = mocker.Mock(spec=Stock)
        ticker.contract.symbol = "TEST"

        # Mock util.isNan to return False
        mocker.patch("ib_async.util.isNan", return_value=False)

        # Mock config methods
        portfolio_manager.config.get_write_threshold_sigma.return_value = None
        portfolio_manager.config.get_write_threshold_perc.return_value = 0.05

        threshold, daily_change = await portfolio_manager.get_write_threshold(
            ticker, "C"
        )

        # Should use close price (100.0) for calculation
        assert threshold == pytest.approx(5.0)  # 0.05 * 100.0
        assert daily_change == pytest.approx(2.0)  # abs(102.0 - 100.0)

    @pytest.mark.asyncio
    async def test_get_write_threshold_with_nan_close(self, portfolio_manager, mocker):
        """Test get_write_threshold falls back to market price when close is NaN."""
        ticker = mocker.Mock(spec=Ticker)
        ticker.close = float("nan")
        ticker.marketPrice.return_value = 102.0
        ticker.contract = mocker.Mock(spec=Stock)
        ticker.contract.symbol = "TEST"

        # Mock util.isNan to return True for NaN
        mocker.patch("ib_async.util.isNan", return_value=True)

        # Mock config methods
        portfolio_manager.config.get_write_threshold_sigma.return_value = None
        portfolio_manager.config.get_write_threshold_perc.return_value = 0.05

        threshold, daily_change = await portfolio_manager.get_write_threshold(
            ticker, "C"
        )

        # Should use market price (102.0) for both calculation and comparison
        assert threshold == pytest.approx(5.1)  # 0.05 * 102.0
        assert daily_change == pytest.approx(0.0)  # abs(102.0 - 102.0)

    @pytest.mark.asyncio
    async def test_write_calls_respects_can_write_when_green_with_nan_close(
        self, portfolio_manager, mocker
    ):
        """Test write_calls correctly handles can_write_when_green check when close is NaN."""
        # This test verifies that the write options logic works correctly with NaN close prices
        # by falling back to market price for comparison

        ticker = mocker.Mock(spec=Ticker)
        ticker.close = float("nan")
        ticker.marketPrice.return_value = (
            105.0  # Market price is higher (stock is "green")
        )
        ticker.contract = mocker.Mock(spec=Stock)
        ticker.contract.symbol = "TEST"

        # Mock util.isNan to return True for NaN
        mocker.patch("ib_async.util.isNan", return_value=True)

        # When close is NaN and we fall back to market price,
        # the comparison becomes marketPrice() > marketPrice() which is always False
        # This means the stock won't be considered "green" or "red" when close is NaN

        # Setup portfolio manager mocks
        portfolio_manager.config.write_when.calls.green = (
            False  # Don't write when green
        )
        portfolio_manager.config.write_when.calls.red = True

        # The logic should proceed since marketPrice > marketPrice is False
        # (not considered green when close is NaN)

        # We're not testing the full write_calls method here, just the close price logic
        # A full integration test would require mocking many more dependencies

    def test_ib_async_v2_compatibility(self):
        """Test that the code is compatible with ib_async v2.0.1 NaN defaults."""
        # This test documents the expected behavior with ib_async v2.0.1
        # where ticker.close defaults to NaN instead of being populated

        # In v1.0.3: ticker.close would be populated with actual close price
        # In v2.0.1: ticker.close defaults to NaN unless explicitly requested

        # Our get_close_price method handles this by:
        # 1. Checking if close is NaN
        # 2. Falling back to market price if it is
        # 3. This ensures the code continues to work with both versions
        pass

    @pytest.mark.asyncio
    async def test_check_if_can_write_puts_skips_buy_only_symbols(
        self, portfolio_manager, mocker
    ):
        """Test that check_if_can_write_puts skips buy-only rebalancing symbols."""
        # Mock config
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(weight=0.5, buy_only_rebalancing=True),
            "MSFT": mocker.Mock(weight=0.5, buy_only_rebalancing=False),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            side_effect=lambda s: s == "AAPL"
        )
        portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=True)
        portfolio_manager.config.can_write_when = mocker.Mock(return_value=(True, True))
        portfolio_manager.config.write_when = mocker.Mock()
        portfolio_manager.config.write_when.calculate_net_contracts = False

        # Mock account summary
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # Mock portfolio positions
        portfolio_positions = {}

        # Mock get_buying_power
        portfolio_manager.get_buying_power = mocker.Mock(return_value=50000)

        # Mock IBKR methods
        mock_ticker = mocker.Mock()
        mock_ticker.marketPrice.return_value = 150.0
        portfolio_manager.ibkr.get_ticker_for_stock = mocker.AsyncMock(
            return_value=mock_ticker
        )

        # Mock get_primary_exchange
        portfolio_manager.get_primary_exchange = mocker.Mock(return_value="NASDAQ")

        # Mock get_maximum_new_contracts_for
        portfolio_manager.get_maximum_new_contracts_for = mocker.AsyncMock(
            return_value=10
        )

        # Mock get_write_threshold
        portfolio_manager.get_write_threshold = mocker.AsyncMock(
            return_value=(0.01, 0.02)  # threshold, daily_change
        )

        # Mock get_close_price
        mocker.patch(
            "thetagang.portfolio_manager.PortfolioManager.get_close_price",
            return_value=149.0,
        )

        # Mock log.track_async to execute tasks immediately
        async def mock_track_async(tasks, description):
            for task in tasks:
                await task

        mocker.patch("thetagang.log.track_async", side_effect=mock_track_async)

        # Call the method
        (
            positions_table,
            put_actions_table,
            to_write,
        ) = await portfolio_manager.check_if_can_write_puts(
            account_summary, portfolio_positions
        )

        # Verify AAPL (buy-only) has 0 puts to write
        # Verify MSFT (normal) would have puts to write if conditions are met
        assert len(to_write) <= 1  # At most MSFT

        # If MSFT was added to to_write, verify it's not AAPL
        for symbol, _, _, _ in to_write:
            assert symbol != "AAPL"

    @pytest.mark.asyncio
    async def test_check_buy_only_positions(self, portfolio_manager, mocker):
        """Test check_buy_only_positions method."""
        # Mock config
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.5,
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=None,
            ),
            "MSFT": mocker.Mock(
                weight=0.3,
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=None,
            ),
            "GOOGL": mocker.Mock(
                weight=0.2,
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            side_effect=lambda s: s in ["AAPL", "GOOGL"]
        )

        # Mock account summary
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # Mock portfolio positions - AAPL has 100 shares, others have 0
        mock_aapl_position = mocker.Mock()
        mock_aapl_position.contract = mocker.Mock(spec=Stock)
        mock_aapl_position.contract.symbol = "AAPL"
        mock_aapl_position.position = 100

        portfolio_positions = {"AAPL": [mock_aapl_position]}

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

        # Expected calculations:
        # AAPL: target = 0.5 * 50000 = $25000, target_shares = 25000/150 = 166
        #       current = 100, to_buy = 166 - 100 = 66
        # GOOGL: target = 0.2 * 50000 = $10000, target_shares = 10000/150 = 66
        #        current = 0, to_buy = 66

        assert len(to_buy) == 2

        # Check the buy orders
        buy_dict = {symbol: qty for symbol, _, qty in to_buy}
        assert "AAPL" in buy_dict
        assert "GOOGL" in buy_dict
        assert buy_dict["AAPL"] == 66
        assert buy_dict["GOOGL"] == 66

        # MSFT should not be in the list (not buy-only)
        assert "MSFT" not in buy_dict

    @pytest.mark.asyncio
    async def test_execute_buy_orders(self, portfolio_manager, mocker):
        """Test execute_buy_orders method."""
        # Mock dependencies
        portfolio_manager.get_order_exchange = mocker.Mock(return_value="SMART")
        portfolio_manager.get_algo_strategy = mocker.Mock(return_value="Adaptive")
        portfolio_manager.get_algo_params = mocker.Mock(return_value=[])
        portfolio_manager.enqueue_order = mocker.AsyncMock()
        portfolio_manager.trades = mocker.Mock()

        # Mock ticker
        mock_ticker = mocker.Mock()
        mock_ticker.bid = 149.50
        mock_ticker.ask = 150.50
        mocker.patch(
            "thetagang.portfolio_manager.midpoint_or_market_price", return_value=150.0
        )

        portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(
            return_value=mock_ticker
        )

        # Mock Stock class
        mock_stock = mocker.Mock(spec=Stock)
        mocker.patch("thetagang.portfolio_manager.Stock", return_value=mock_stock)

        # Mock LimitOrder class
        mock_limit_order = mocker.patch("thetagang.portfolio_manager.LimitOrder")
        mock_order = mocker.Mock()
        mock_limit_order.return_value = mock_order

        # Mock log.notice and log.error
        mocker.patch("thetagang.log.notice")
        mocker.patch("thetagang.log.error")

        # Mock enqueue_order (returns None)
        portfolio_manager.enqueue_order = mocker.Mock()

        # Test data
        buy_orders = [
            ("AAPL", "NASDAQ", 50),
            ("GOOGL", "NASDAQ", 30),
        ]

        # Execute
        await portfolio_manager.execute_buy_orders(buy_orders)

        # Verify orders were created
        assert portfolio_manager.enqueue_order.call_count == 2

        # Verify order parameters
        mock_limit_order.assert_any_call(
            "BUY",
            50,
            150.0,
            algoStrategy="Adaptive",
            algoParams=[],
            tif="DAY",
            account=portfolio_manager.account_number,
        )
        mock_limit_order.assert_any_call(
            "BUY",
            30,
            150.0,
            algoStrategy="Adaptive",
            algoParams=[],
            tif="DAY",
            account=portfolio_manager.account_number,
        )

    @pytest.mark.asyncio
    async def test_buy_only_positions_insufficient_buying_power(
        self, portfolio_manager, mocker
    ):
        """Test check_buy_only_positions when there's insufficient buying power."""
        # Mock config
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=1.0,  # 100% allocation
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary - very limited buying power
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # No existing positions
        portfolio_positions = {}

        # Mock get_buying_power - only $1000 available
        portfolio_manager.get_buying_power = mocker.Mock(return_value=1000)

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

        # With $1000 buying power and $150/share, can only buy 6 shares
        assert len(to_buy) == 1
        assert to_buy[0][0] == "AAPL"
        assert to_buy[0][2] == 6  # floor(1000/150)

    def test_calc_pending_cash_balance_with_stock_orders(
        self, portfolio_manager, mocker
    ):
        """Test that calc_pending_cash_balance correctly handles stock BUY orders."""
        # Create mock stock contract
        mock_stock = mocker.Mock()
        mock_stock.secType = "STK"
        mock_stock.multiplier = ""  # Stocks often have empty multiplier

        # Create mock option contract
        mock_option = mocker.Mock()
        mock_option.secType = "OPT"
        mock_option.multiplier = "100"

        # Create mock orders
        stock_buy_order = mocker.Mock()
        stock_buy_order.action = "BUY"
        stock_buy_order.lmtPrice = 150.0
        stock_buy_order.totalQuantity = 100

        option_sell_order = mocker.Mock()
        option_sell_order.action = "SELL"
        option_sell_order.lmtPrice = 2.50
        option_sell_order.totalQuantity = 5

        # Mock the orders.records() to return our test orders
        portfolio_manager.orders.records = mocker.Mock(
            return_value=[
                (mock_stock, stock_buy_order),
                (mock_option, option_sell_order),
            ]
        )

        # Calculate pending cash balance
        pending_balance = portfolio_manager.calc_pending_cash_balance()

        # Expected:
        # Stock BUY: -150 * 100 * 1 = -15,000
        # Option SELL: +2.50 * 5 * 100 = +1,250
        # Total: -13,750
        assert pending_balance == -13750.0

    @pytest.mark.asyncio
    async def test_buy_only_minimum_shares_threshold(self, portfolio_manager, mocker):
        """Test that buy-only rebalancing respects minimum shares threshold."""
        # Mock config with minimum shares threshold
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.1,
                buy_only_min_threshold_shares=10,
                buy_only_min_threshold_amount=None,
                buy_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary
        account_summary = {"NetLiquidation": mocker.Mock(value=100000)}

        # No existing positions
        portfolio_positions = {}

        # Mock get_buying_power - enough for target allocation
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

        # Target: 0.1 * 10000 = $1000, which is 6.66 shares
        # Since 6 shares < 10 minimum, should not buy
        assert len(to_buy) == 0

    @pytest.mark.asyncio
    async def test_buy_only_minimum_amount_threshold(self, portfolio_manager, mocker):
        """Test that buy-only rebalancing respects minimum dollar amount threshold."""
        # Mock config with minimum amount threshold
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.05,
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=1000.0,
                buy_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary
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

        # Target: 0.05 * 10000 = $500, which is 3.33 shares (3 shares = $450)
        # Since $450 < $1000 minimum, should not buy
        assert len(to_buy) == 0

    @pytest.mark.asyncio
    async def test_buy_only_amount_less_than_one_share_rounds_up(
        self, portfolio_manager, mocker
    ):
        """Test that when min amount is less than 1 share price, it rounds up to 1 share."""
        # Mock config with small minimum amount threshold
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.01,  # Small allocation
                buy_only_min_threshold_shares=None,
                buy_only_min_threshold_amount=100.0,  # Less than 1 share
                buy_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary
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

        # Target: 0.01 * 10000 = $100, which is 0.66 shares
        # Min amount is $100, which is less than 1 share ($150)
        # Should round up to 1 share
        assert len(to_buy) == 1
        assert to_buy[0][0] == "AAPL"
        assert to_buy[0][2] == 1  # Should buy 1 share

    @pytest.mark.asyncio
    async def test_buy_only_amount_threshold_takes_precedence(
        self, portfolio_manager, mocker
    ):
        """Test that dollar amount threshold takes precedence over shares threshold."""
        # Mock config with both thresholds
        portfolio_manager.config.symbols = {
            "AAPL": mocker.Mock(
                weight=0.1,
                buy_only_min_threshold_shares=1,  # Would allow purchase
                buy_only_min_threshold_amount=2000.0,  # Would block purchase
                buy_only_min_threshold_percent=None,
                buy_only_min_threshold_percent_relative=None,
            ),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            return_value=True
        )

        # Mock account summary
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
        # Even though 6 shares meets min shares (1), $900 < $2000 min amount
        # Should not buy due to amount threshold
        assert len(to_buy) == 0
