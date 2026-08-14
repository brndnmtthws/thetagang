from unittest.mock import Mock

import pytest
from ib_async import Contract, LimitOrder, Trade

from thetagang.ibkr import IBKR
from thetagang.trades import Trades


@pytest.fixture
def mock_ibkr() -> Mock:
    return Mock(spec=IBKR)


@pytest.fixture
def trades(mock_ibkr: Mock) -> Trades:
    return Trades(mock_ibkr)


@pytest.fixture
def mock_contract() -> Mock:
    return Mock(spec=Contract)


@pytest.fixture
def mock_order() -> Mock:
    return Mock(spec=LimitOrder)


@pytest.fixture
def mock_trade() -> Mock:
    return Mock(spec=Trade)


def test_submit_order_successful(
    trades: Trades,
    mock_contract: Mock,
    mock_order: Mock,
    mock_trade: Mock,
    mock_ibkr: Mock,
) -> None:
    mock_ibkr.place_order.return_value = mock_trade
    assert trades.submit_order(mock_contract, mock_order) is True
    mock_ibkr.place_order.assert_called_once_with(mock_contract, mock_order)
    assert len(trades.records()) == 1
    assert trades.records()[0] == mock_trade


def test_submit_order_with_replacement(
    trades: Trades,
    mock_contract: Mock,
    mock_order: Mock,
    mock_trade: Mock,
    mock_ibkr: Mock,
) -> None:
    mock_ibkr.place_order.return_value = mock_trade
    trades.submit_order(mock_contract, mock_order)
    new_trade = Mock(spec=Trade)
    mock_ibkr.place_order.return_value = new_trade
    trades.submit_order(mock_contract, mock_order, idx=0)
    assert len(trades.records()) == 1
    assert trades.records()[0] == new_trade


def test_submit_order_failure(
    trades: Trades, mock_contract: Mock, mock_order: Mock, mock_ibkr: Mock
) -> None:
    mock_ibkr.place_order.side_effect = RuntimeError("Failed to place order")
    assert trades.submit_order(mock_contract, mock_order) is False
    mock_ibkr.place_order.assert_called_once_with(mock_contract, mock_order)
    assert len(trades.records()) == 0


def test_submit_order_connection_failure(
    trades: Trades, mock_contract: Mock, mock_order: Mock, mock_ibkr: Mock
) -> None:
    mock_ibkr.place_order.side_effect = ConnectionError("Not connected")

    assert trades.submit_order(mock_contract, mock_order) is False
    mock_ibkr.place_order.assert_called_once_with(mock_contract, mock_order)
    assert trades.records() == []


def test_submit_order_does_not_swallow_base_exception(
    trades: Trades, mock_contract: Mock, mock_order: Mock, mock_ibkr: Mock
) -> None:
    class FatalPlacementError(BaseException):
        pass

    error = FatalPlacementError()
    mock_ibkr.place_order.side_effect = error

    with pytest.raises(FatalPlacementError) as exc_info:
        trades.submit_order(mock_contract, mock_order)

    assert exc_info.value is error


def test_submit_order_multiple_trades(
    trades: Trades,
    mock_contract: Mock,
    mock_order: Mock,
    mock_trade: Mock,
    mock_ibkr: Mock,
) -> None:
    mock_ibkr.place_order.return_value = mock_trade
    trades.submit_order(mock_contract, mock_order)
    trades.submit_order(mock_contract, mock_order)
    assert mock_ibkr.place_order.call_count == 2
    assert len(trades.records()) == 2
