import asyncio
from types import SimpleNamespace

import pytest
from ib_async import (
    IB,
    AccountValue,
    Contract,
    Index,
    Order,
    OrderStatus,
    Stock,
    Ticker,
    Trade,
)

from thetagang import log
from thetagang.ibkr import (
    IBKR,
    IBKRRequestTimeout,
    RequiredFieldValidationError,
    TickerField,
)

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
    mock.wrapper = mocker.Mock()
    mock.wrapper.accountValues = {}
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
    contract.conId = 1
    mocker.patch.object(
        ibkr, "qualify_contracts", new=mocker.AsyncMock(return_value=[contract])
    )
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
    contract.conId = 1
    mocker.patch.object(
        ibkr, "qualify_contracts", new=mocker.AsyncMock(return_value=[contract])
    )
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


async def test_get_ticker_for_stock_falls_back_to_index(ibkr, mock_ticker, mocker):
    """Fallback to an index contract when stock qualification fails."""
    index_contract = Index("SPX", "CBOE", "USD")
    index_contract.conId = 123

    qualify_contracts = mocker.AsyncMock(side_effect=[[], [index_contract]])
    mocker.patch.object(ibkr, "qualify_contracts", new=qualify_contracts)
    get_ticker = mocker.patch.object(
        ibkr, "get_ticker_for_contract", new=mocker.AsyncMock(return_value=mock_ticker)
    )

    result = await ibkr.get_ticker_for_stock("SPX", "CBOE")

    assert result == mock_ticker
    assert get_ticker.await_count == 1
    called_contract = get_ticker.await_args.args[0]
    assert isinstance(called_contract, Index)
    assert called_contract.symbol == "SPX"
    assert called_contract.conId == 123


async def test_market_data_streaming_handler_requires_conid(ibkr, mock_ib, mocker):
    """Raise when contract can't be qualified to a conId."""
    mocker.patch.object(
        ibkr, "qualify_contracts", new=mocker.AsyncMock(return_value=[])
    )
    mock_ib.reqMktData = mocker.Mock()

    contract = Stock("TEST", "SMART", "USD")
    contract.conId = 0

    async def handler(_ticker):
        return None

    with pytest.raises(ValueError) as excinfo:
        await ibkr.__market_data_streaming_handler__(contract, "", handler)

    assert "no 'conId' value exists" in str(excinfo.value)
    mock_ib.reqMktData.assert_not_called()


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


async def test_refresh_account_updates_uses_timeout_wrapper(ibkr, mocker):
    """refresh_account_updates delegates to _await_with_timeout."""
    req_future: asyncio.Future = asyncio.get_running_loop().create_future()
    req_future.set_result(None)
    ibkr.ib.reqAccountUpdatesAsync = mocker.Mock(return_value=req_future)
    mocker.patch.object(ibkr, "_account_snapshot_ready", side_effect=[False, True])
    await_wrapper = mocker.patch.object(
        ibkr, "_await_with_timeout", new=mocker.AsyncMock(return_value=None)
    )

    await ibkr.refresh_account_updates("ACC123")

    ibkr.ib.reqAccountUpdatesAsync.assert_called_once_with("ACC123")
    assert await_wrapper.await_count == 1
    await_args = await_wrapper.await_args
    assert await_args.args[0] is req_future
    assert await_args.args[1] == "account updates"


async def test_refresh_positions_uses_timeout_wrapper(ibkr, mocker):
    """refresh_positions delegates to _await_with_timeout."""
    req_future: asyncio.Future = asyncio.get_running_loop().create_future()
    req_future.set_result([])
    ibkr.ib.reqPositionsAsync = mocker.Mock(return_value=req_future)
    await_wrapper = mocker.patch.object(
        ibkr, "_await_with_timeout", new=mocker.AsyncMock(return_value=[])
    )

    result = await ibkr.refresh_positions()

    assert result == []
    ibkr.ib.reqPositionsAsync.assert_called_once_with()
    assert await_wrapper.await_count == 1
    await_args = await_wrapper.await_args
    assert await_args.args[0] is req_future
    assert await_args.args[1] == "positions snapshot"


async def test_refresh_account_updates_propagates_timeout(ibkr, mocker):
    """refresh_account_updates re-raises IBKRRequestTimeout."""
    ibkr.ib.reqAccountUpdatesAsync = mocker.Mock(return_value=object())
    mocker.patch.object(
        ibkr,
        "_await_with_timeout",
        new=mocker.AsyncMock(
            side_effect=IBKRRequestTimeout(
                "account updates", ibkr.api_response_wait_time
            )
        ),
    )

    with pytest.raises(IBKRRequestTimeout):
        await ibkr.refresh_account_updates("ACC123")


async def test_refresh_account_updates_skips_when_snapshot_ready(ibkr, mocker):
    """No request issued when account snapshot already populated."""
    mocker.patch.object(ibkr, "_account_snapshot_ready", return_value=True)

    await ibkr.refresh_account_updates("ACC123")

    ibkr.ib.reqAccountUpdatesAsync.assert_not_called()


async def test_refresh_account_updates_allows_timeout_if_data_ready(ibkr, mocker):
    """A timeout is ignored when snapshot becomes ready while waiting."""
    mocker.patch.object(ibkr, "_account_snapshot_ready", side_effect=[False, True])
    ibkr.ib.reqAccountUpdatesAsync = mocker.Mock(return_value=object())
    mocker.patch.object(
        ibkr,
        "_await_with_timeout",
        new=mocker.AsyncMock(
            side_effect=IBKRRequestTimeout(
                "account updates", ibkr.api_response_wait_time
            )
        ),
    )

    await ibkr.refresh_account_updates("ACC123")

    assert ibkr._account_snapshot_ready.call_count == 2


async def test_refresh_account_updates_raises_when_snapshot_never_populates(
    ibkr, mocker
):
    """If data never arrives, an IBKRRequestTimeout is raised."""
    mocker.patch.object(ibkr, "_account_snapshot_ready", return_value=False)
    ibkr.ib.reqAccountUpdatesAsync = mocker.Mock(return_value=object())
    mocker.patch.object(
        ibkr, "_await_with_timeout", new=mocker.AsyncMock(return_value=None)
    )

    with pytest.raises(IBKRRequestTimeout) as excinfo:
        await ibkr.refresh_account_updates("ACC123")

    assert "no usable account values" in str(excinfo.value)


async def test_account_snapshot_ready_checks_for_non_zero_account_values(ibkr, mock_ib):
    """Helper returns True only when tracked tags have non-zero data."""
    mock_ib.wrapper.accountValues = {
        ("ACC123", "NetLiquidation", "USD", ""): AccountValue(
            "ACC123", "NetLiquidation", "0", "USD", ""
        )
    }

    assert ibkr._account_snapshot_ready("ACC123") is False

    mock_ib.wrapper.accountValues = {
        ("ACC123", "NetLiquidation", "USD", ""): AccountValue(
            "ACC123", "NetLiquidation", "100000", "USD", ""
        )
    }

    assert ibkr._account_snapshot_ready("ACC123") is True


async def test_account_snapshot_ready_ignores_other_accounts_and_tags(ibkr, mock_ib):
    """Values for other accounts or untracked tags should not mark snapshot ready."""
    mock_ib.wrapper.accountValues = {
        ("OTHER", "NetLiquidation", "USD", ""): AccountValue(
            "OTHER", "NetLiquidation", "100000", "USD", ""
        ),
        ("ACC123", "GrossPositionValue", "USD", ""): AccountValue(
            "ACC123", "GrossPositionValue", "5000", "USD", ""
        ),
    }

    assert ibkr._account_snapshot_ready("ACC123") is False


async def test_account_snapshot_ready_handles_missing_wrapper_or_values(ibkr, mock_ib):
    """Return False when wrapper or accountValues are absent."""
    mock_ib.wrapper.accountValues = {}
    assert ibkr._account_snapshot_ready("ACC123") is False

    mock_ib.wrapper = None
    assert ibkr._account_snapshot_ready("ACC123") is False


async def test_account_value_has_data_true_for_non_zero_numeric(ibkr):
    """Helper treats any non-zero numeric string as usable data."""
    value = AccountValue("ACC123", "NetLiquidation", "123.45", "USD", "")
    assert ibkr._account_value_has_data(value) is True


@pytest.mark.parametrize("raw_value", ["0", "0.0", "", None, "abc"])
async def test_account_value_has_data_false_for_invalid_inputs(ibkr, raw_value):
    """Helper rejects zero, empty, None, and non-numeric values."""
    value = SimpleNamespace(value=raw_value)

    assert ibkr._account_value_has_data(value) is False


async def test_await_with_timeout_wraps_timeout_error(ibkr, mocker):
    """_await_with_timeout raises IBKRRequestTimeout on asyncio timeout."""

    async def dummy() -> None:
        return None

    async def fake_wait_for(awaitable, timeout):
        await awaitable
        raise asyncio.TimeoutError()

    mocker.patch("thetagang.ibkr.asyncio.wait_for", new=fake_wait_for)

    with pytest.raises(IBKRRequestTimeout) as excinfo:
        await ibkr._await_with_timeout(dummy(), "positions snapshot")

    assert "positions snapshot" in str(excinfo.value)
