import asyncio

import pytest
from ib_async import IB, Contract, Order, OrderStatus, Stock, Ticker, Trade

from thetagang import log
from thetagang.ibkr import IBKR, RequiredFieldValidationError, TickerField

# Mark all tests in this module as asyncio
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_ib(mocker):
    """Fixture to create a mock IB object."""
    mock = mocker.Mock(spec=IB)
    # Add the missing event attribute needed by IBKR.__init__
    # Make it a simple mock that accepts the += operation.
    mock.orderStatusEvent = mocker.Mock()
    mock.orderStatusEvent.__iadd__ = mocker.Mock(
        return_value=None
    )  # Allow += operation
    return mock


@pytest.fixture
def ibkr(mock_ib):
    """Fixture to create an IBKR instance with a mock IB."""
    return IBKR(ib=mock_ib, api_response_wait_time=1, default_order_exchange="SMART")


@pytest.fixture
def mock_ticker(mocker):
    """Fixture to create a mock Ticker object."""
    ticker = mocker.Mock(spec=Ticker)
    ticker.contract = mocker.Mock(spec=Contract)
    ticker.contract.localSymbol = "TEST"
    ticker.contract.symbol = "TEST"
    return ticker


@pytest.fixture
def mock_trade(mocker):
    """Fixture to create a mock Trade object."""
    trade = mocker.Mock(spec=Trade)
    trade.contract = mocker.Mock(spec=Contract)
    trade.contract.symbol = "TEST"
    trade.order = mocker.Mock(spec=Order)
    trade.order.orderId = 123
    trade.orderStatus = mocker.Mock(spec=OrderStatus)
    return trade


# --- Tests for get_ticker_for_contract ---


async def test_get_ticker_for_contract_success(ibkr, mock_ib, mock_ticker, mocker):
    """Test get_ticker_for_contract when all waits succeed."""
    mocker.patch.object(
        ibkr, "__market_data_streaming_handler__", return_value=mock_ticker
    )
    # Mock the internal wait methods to return True (success)
    mocker.patch.object(
        ibkr, "__ticker_wait_for_condition__", return_value=asyncio.Future()
    )
    ibkr.__ticker_wait_for_condition__.return_value.set_result(True)

    contract = Stock("TEST", "SMART", "USD")
    result = await ibkr.get_ticker_for_contract(
        contract,
        required_fields=[TickerField.MARKET_PRICE],
        optional_fields=[TickerField.MIDPOINT],
    )

    assert result == mock_ticker
    # Check that the wait was attempted (indirectly, via the handler logic patch)
    ibkr.__market_data_streaming_handler__.assert_awaited_once()


async def test_get_ticker_for_contract_required_timeout(
    ibkr, mock_ib, mock_ticker, mocker
):
    """Test get_ticker_for_contract when a required field wait times out."""
    # Mock ib methods called by __market_data_streaming_handler__
    mock_ib.qualifyContractsAsync = mocker.AsyncMock()
    mock_ib.reqMktData = mocker.Mock(return_value=mock_ticker)

    # Mock __ticker_field_handler__ to return appropriate async functions
    async def succeed_wait(ticker):
        return True

    async def fail_wait(ticker):
        return False

    def mock_handler_logic(field):
        if field == TickerField.MARKET_PRICE:  # Required field
            return fail_wait
        elif field == TickerField.MIDPOINT:  # Optional field
            return succeed_wait
        else:
            pytest.fail(f"Unexpected field: {field}")

    mocker.patch.object(
        ibkr, "__ticker_field_handler__", side_effect=mock_handler_logic
    )

    contract = Stock("TEST", "SMART", "USD")
    with pytest.raises(RequiredFieldValidationError) as excinfo:
        await ibkr.get_ticker_for_contract(
            contract,
            required_fields=[TickerField.MARKET_PRICE],
            optional_fields=[TickerField.MIDPOINT],
        )

    assert "Required fields timed out" in str(excinfo.value)
    assert "MARKET_PRICE" in str(excinfo.value)
    # Ensure the handler was called for both fields
    assert ibkr.__ticker_field_handler__.call_count == 2


async def test_get_ticker_for_contract_optional_timeout(
    ibkr, mock_ib, mock_ticker, mocker
):
    """Test get_ticker_for_contract when an optional field wait times out."""
    # Mock ib methods called by __market_data_streaming_handler__
    mock_ib.qualifyContractsAsync = mocker.AsyncMock()
    mock_ib.reqMktData = mocker.Mock(return_value=mock_ticker)
    mock_log_warning = mocker.patch.object(log, "warning")

    # Mock __ticker_field_handler__ to return appropriate async functions
    async def succeed_wait(ticker):
        return True

    async def fail_wait(ticker):
        return False

    def mock_handler_logic(field):
        if field == TickerField.MARKET_PRICE:  # Required field
            return succeed_wait
        elif field == TickerField.MIDPOINT:  # Optional field
            return fail_wait
        else:
            pytest.fail(f"Unexpected field: {field}")

    mocker.patch.object(
        ibkr, "__ticker_field_handler__", side_effect=mock_handler_logic
    )

    contract = Stock("TEST", "SMART", "USD")
    result = await ibkr.get_ticker_for_contract(
        contract,
        required_fields=[TickerField.MARKET_PRICE],
        optional_fields=[TickerField.MIDPOINT],
    )

    assert result == mock_ticker
    # Ensure the handler was called for both fields
    assert ibkr.__ticker_field_handler__.call_count == 2
    mock_log_warning.assert_called_once()
    assert "Optional fields timed out" in mock_log_warning.call_args[0][0]
    assert "MIDPOINT" in mock_log_warning.call_args[0][0]


# --- Tests for wait_for_submitting_orders ---


async def test_wait_for_submitting_orders_success(ibkr, mock_trade, mocker):
    """Test wait_for_submitting_orders when all waits succeed."""
    mocker.patch.object(
        ibkr, "__trade_wait_for_condition__", return_value=asyncio.Future()
    )
    ibkr.__trade_wait_for_condition__.return_value.set_result(True)
    mocker.patch.object(
        log, "track_async", return_value=[True, True]
    )  # Simulate track_async returning results

    trades = [mock_trade, mock_trade]
    await ibkr.wait_for_submitting_orders(trades)

    assert ibkr.__trade_wait_for_condition__.call_count == 2


async def test_wait_for_submitting_orders_timeout(ibkr, mock_trade, mocker):
    """Test wait_for_submitting_orders when a wait times out."""

    # Mock the wait to return False for the second trade
    async def mock_wait(*args, **kwargs):
        # Simulate different results based on call order or trade details if needed
        # Simple case: first succeeds, second fails
        if ibkr.__trade_wait_for_condition__.call_count == 1:
            return True
        else:
            return False

    mocker.patch.object(ibkr, "__trade_wait_for_condition__", side_effect=mock_wait)
    # Mock track_async to return the results from our side_effect
    mocker.patch.object(log, "track_async", return_value=[True, False])

    trades = [mocker.Mock(spec=Trade), mocker.Mock(spec=Trade)]
    trades[0].contract = mocker.Mock(spec=Contract)
    trades[0].contract.symbol = "PASS"
    trades[0].order = mocker.Mock(spec=Order)
    trades[0].order.orderId = 1
    trades[1].contract = mocker.Mock(spec=Contract)
    trades[1].contract.symbol = "FAIL"
    trades[1].order = mocker.Mock(spec=Order)
    trades[1].order.orderId = 2

    with pytest.raises(RuntimeError) as excinfo:
        await ibkr.wait_for_submitting_orders(trades)

    assert "Timeout waiting for orders to submit" in str(excinfo.value)
    assert "FAIL (OrderId: 2)" in str(excinfo.value)
    assert "PASS (OrderId: 1)" not in str(excinfo.value)
    assert ibkr.__trade_wait_for_condition__.call_count == 2


# --- Tests for wait_for_orders_complete ---


async def test_wait_for_orders_complete_success(ibkr, mock_trade, mocker):
    """Test wait_for_orders_complete when all waits succeed."""
    mocker.patch.object(
        ibkr, "__trade_wait_for_condition__", return_value=asyncio.Future()
    )
    ibkr.__trade_wait_for_condition__.return_value.set_result(True)
    mocker.patch.object(log, "track_async", return_value=[True, True])
    mock_log_warning = mocker.patch.object(log, "warning")

    trades = [mock_trade, mock_trade]
    await ibkr.wait_for_orders_complete(trades)

    assert ibkr.__trade_wait_for_condition__.call_count == 2
    mock_log_warning.assert_not_called()


async def test_wait_for_orders_complete_timeout(ibkr, mock_trade, mocker):
    """Test wait_for_orders_complete when a wait times out."""

    # Mock the wait to return False for the second trade
    async def mock_wait(*args, **kwargs):
        if ibkr.__trade_wait_for_condition__.call_count == 1:
            return True
        else:
            return False

    mocker.patch.object(ibkr, "__trade_wait_for_condition__", side_effect=mock_wait)
    mocker.patch.object(log, "track_async", return_value=[True, False])
    mock_log_warning = mocker.patch.object(log, "warning")

    trades = [mocker.Mock(spec=Trade), mocker.Mock(spec=Trade)]
    trades[0].contract = mocker.Mock(spec=Contract)
    trades[0].contract.symbol = "PASS"
    trades[0].order = mocker.Mock(spec=Order)
    trades[0].order.orderId = 1
    trades[1].contract = mocker.Mock(spec=Contract)
    trades[1].contract.symbol = "FAIL"
    trades[1].order = mocker.Mock(spec=Order)
    trades[1].order.orderId = 2

    await ibkr.wait_for_orders_complete(trades)

    assert ibkr.__trade_wait_for_condition__.call_count == 2
    mock_log_warning.assert_called_once()
    assert "Timeout waiting for orders to complete" in mock_log_warning.call_args[0][0]
    assert "FAIL (OrderId: 2)" in mock_log_warning.call_args[0][0]
    assert "PASS (OrderId: 1)" not in mock_log_warning.call_args[0][0]
