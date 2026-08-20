from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from ib_async import IB, AccountValue, LimitOrder, Option, Stock, Ticker

from thetagang.db import DataStore
from thetagang.portfolio_manager import PortfolioManager
from thetagang.strategies.tail_hedge_state import (
    TAIL_HEDGE_MIN_LIMIT_PRICE_ATTR,
    TailHedgeCohort,
    TailHedgeState,
    TailHedgeStateStore,
)


def _naive_utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    """Build the naive UTC values used by persisted broker state."""
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC).replace(
        tzinfo=None
    )


@pytest.fixture
def mock_ib(mocker):
    """Fixture to create a mock IB object."""
    mock = mocker.Mock(spec=IB)
    mock.orderStatusEvent = mocker.Mock()
    mock.orderStatusEvent.__iadd__ = mocker.Mock(return_value=None)
    mock.openTrades.return_value = []
    return mock


@pytest.fixture
def mock_config(mocker):
    """Fixture to create a mock Config object."""
    config = mocker.Mock()
    config.runtime.account = mocker.Mock()
    config.runtime.account.number = "TEST123"
    config.runtime.ib_async = mocker.Mock()
    config.runtime.ib_async.api_response_wait_time = 1
    config.runtime.orders = mocker.Mock()
    config.runtime.orders.exchange = "SMART"
    config.runtime.orders.estimated_fee_per_contract = 0.0
    config.strategies.cash_management = mocker.Mock()
    config.strategies.cash_management.cash_fund = "MMDA1"
    return config


@pytest.fixture
def portfolio_manager(mock_ib, mock_config, mocker):
    """Fixture to create a PortfolioManager instance."""
    completion_future = mocker.Mock()
    return PortfolioManager(mock_config, mock_ib, completion_future, dry_run=False)


def position(portfolio_manager, contract, quantity, account=None):
    return SimpleNamespace(
        account=account or portfolio_manager.account_number,
        contract=contract,
        position=quantity,
    )


def tail_order(portfolio_manager, action, quantity, order_ref):
    return LimitOrder(
        action,
        quantity,
        1.0,
        account=portfolio_manager.account_number,
        orderRef=order_ref,
    )


def persist_tail_entry(portfolio_manager, tmp_path, contract):
    data_store = DataStore(
        f"sqlite:///{tmp_path / 'tail-entry.db'}",
        str(tmp_path / "config.toml"),
        dry_run=False,
        config_text="config",
    )
    portfolio_manager.data_store = data_store
    store = TailHedgeStateStore(data_store, portfolio_manager.account_number)
    entered_at = _naive_utc(2026, 8, 15, 12)
    store.save(
        TailHedgeState(
            [
                TailHedgeCohort(
                    entry_id=(
                        f"{contract.symbol}:{contract.conId}:{entered_at.isoformat()}"
                    ),
                    symbol=contract.symbol,
                    status="entry_enqueued",
                    con_id=contract.conId,
                    expiration=contract.lastTradeDateOrContractMonth,
                    strike=float(contract.strike),
                    quantity=1,
                    entry_limit_price=1.0,
                    entered_at=entered_at,
                    estimated_cost=100.0,
                )
            ]
        )
    )
    return store


def working_trade(mocker, portfolio_manager, *, order_ref="", account=None):
    order = LimitOrder(
        "BUY",
        1,
        1.0,
        account=account or portfolio_manager.account_number,
        orderRef=order_ref,
    )
    trade = mocker.Mock(contract=mocker.Mock(symbol="SPY"), order=order)
    trade.isDone.return_value = False
    return trade


def prepare_initialization(portfolio_manager, mocker, trades):
    portfolio_manager.config.runtime.account.market_data_type = 1
    portfolio_manager.config.runtime.account.cancel_orders = True
    portfolio_manager.config.strategies.vix_call_hedge.enabled = False
    portfolio_manager.config.strategies.cash_management.enabled = False
    portfolio_manager.get_symbols = mocker.Mock(return_value=["SPY"])
    portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=trades)
    portfolio_manager.ibkr.cancel_order = mocker.Mock()


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

    def test_stage_enabled_defaults_to_known_stages_when_flags_missing(
        self, mock_ib, mock_config, mocker
    ):
        completion_future = mocker.Mock()
        pm = PortfolioManager(mock_config, mock_ib, completion_future, dry_run=True)
        assert pm.stage_enabled("options_write_puts") is True

    def test_stage_enabled_is_false_for_unknown_stage(
        self, mock_ib, mock_config, mocker
    ):
        completion_future = mocker.Mock()
        pm = PortfolioManager(mock_config, mock_ib, completion_future, dry_run=True)
        assert pm.stage_enabled("nonexistent_stage") is False

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

    def test_initialize_account_only_cancels_orders_for_active_account(
        self, portfolio_manager, mocker
    ):
        active = working_trade(mocker, portfolio_manager)
        other = working_trade(
            mocker,
            portfolio_manager,
            account="OTHER123",
        )
        prepare_initialization(portfolio_manager, mocker, [active, other])

        portfolio_manager.initialize_account()

        portfolio_manager.ibkr.cancel_order.assert_called_once_with(active.order)

    def test_initialize_account_dry_run_does_not_cancel_broker_orders(
        self, mock_ib, mock_config, mocker
    ):
        portfolio_manager = PortfolioManager(
            mock_config,
            mock_ib,
            mocker.Mock(),
            dry_run=True,
        )
        trade = working_trade(mocker, portfolio_manager)
        prepare_initialization(portfolio_manager, mocker, [trade])
        cancel_order = mocker.Mock()
        portfolio_manager.ibkr.cancel_order = cancel_order

        portfolio_manager.initialize_account()

        cancel_order.assert_not_called()

    def test_initialize_account_cancels_tail_entry_and_preserves_reductions(
        self, portfolio_manager, mocker
    ):
        refs = [
            "tg:tail-hedge:entry",
            "tg:tail-hedge:close",
            "tg:tail-harvest:SPY:123",
            "tg:regime-rebalance:SPY",
        ]
        trades = [
            working_trade(mocker, portfolio_manager, order_ref=order_ref)
            for order_ref in refs
        ]
        prepare_initialization(portfolio_manager, mocker, trades)

        portfolio_manager.initialize_account()

        canceled_refs = {
            call.args[0].orderRef
            for call in portfolio_manager.ibkr.cancel_order.call_args_list
        }
        assert canceled_refs == {
            "tg:tail-hedge:entry",
            "tg:regime-rebalance:SPY",
        }

    def test_initialize_account_cancels_tail_entry_when_cancel_orders_disabled(
        self, portfolio_manager, mocker
    ):
        entry = working_trade(
            mocker,
            portfolio_manager,
            order_ref="tg:tail-hedge:entry",
        )
        close = working_trade(
            mocker,
            portfolio_manager,
            order_ref="tg:tail-hedge:close",
        )
        prepare_initialization(portfolio_manager, mocker, [entry, close])
        portfolio_manager.config.runtime.account.cancel_orders = False

        portfolio_manager.initialize_account()

        portfolio_manager.ibkr.cancel_order.assert_called_once_with(entry.order)

    def test_submit_orders_caps_tail_sales_to_uncommitted_live_position(
        self, portfolio_manager, mocker
    ):
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        portfolio_manager.ibkr.portfolio = mocker.Mock(
            return_value=[position(portfolio_manager, contract, 3)]
        )
        working_trades = [
            SimpleNamespace(
                contract=contract,
                order=LimitOrder("SELL", quantity, 1.0, account=account),
                isDone=lambda: False,
            )
            for account, quantity in [
                (portfolio_manager.account_number, 2),
                ("OTHER", 99),
            ]
        ]
        portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=working_trades)
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.submit_order.return_value = False
        update_recovery = mocker.patch.object(
            portfolio_manager,
            "_update_tail_recovery_submission",
            return_value=True,
        )
        first = tail_order(portfolio_manager, "SELL", 2, "tg:tail-harvest:SPY:123")
        portfolio_manager.orders.add_order(contract, first, None)

        portfolio_manager.submit_orders()

        portfolio_manager.trades.submit_order.assert_called_once()
        submitted_order = portfolio_manager.trades.submit_order.call_args.args[1]
        assert submitted_order is not first
        assert int(submitted_order.totalQuantity) == 1
        assert [call.args for call in update_recovery.call_args_list] == [
            (123, 1),
            (123, None),
        ]
        assert [call.kwargs for call in update_recovery.call_args_list] == [
            {"live_quantity": 3},
            {},
        ]

    def test_submit_orders_allows_one_tail_sale_per_contract(
        self, portfolio_manager, mocker
    ):
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        portfolio_manager.ibkr.portfolio = mocker.Mock(
            return_value=[position(portfolio_manager, contract, 3)]
        )
        portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.submit_order.return_value = True
        update_recovery = mocker.patch.object(
            portfolio_manager,
            "_update_tail_recovery_submission",
            return_value=True,
        )
        order = tail_order(portfolio_manager, "SELL", 1, "tg:tail-hedge:close")
        portfolio_manager.orders.add_order(contract, order, None)
        portfolio_manager.orders.add_order(contract, order, None)

        portfolio_manager.submit_orders()

        portfolio_manager.trades.submit_order.assert_called_once()
        update_recovery.assert_called_once_with(123, 1, live_quantity=3)

    def test_submit_orders_releases_tail_sale_with_no_live_capacity(
        self, portfolio_manager, mocker
    ):
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        portfolio_manager.ibkr.portfolio = mocker.Mock(return_value=[])
        portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
        portfolio_manager.trades = mocker.Mock()
        update_recovery = mocker.patch.object(
            portfolio_manager,
            "_update_tail_recovery_submission",
            return_value=True,
        )
        portfolio_manager.orders.add_order(
            contract,
            tail_order(portfolio_manager, "SELL", 1, "tg:tail-hedge:close"),
            None,
        )

        portfolio_manager.submit_orders()

        update_recovery.assert_called_once_with(123, None)
        portfolio_manager.trades.submit_order.assert_not_called()

    def test_submit_orders_preserves_tail_sale_consumed_by_working_order(
        self, portfolio_manager, mocker
    ):
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        portfolio_manager.ibkr.portfolio = mocker.Mock(
            return_value=[position(portfolio_manager, contract, 1)]
        )
        working = SimpleNamespace(
            contract=contract,
            order=LimitOrder(
                "SELL",
                1,
                1.0,
                account=portfolio_manager.account_number,
            ),
            isDone=lambda: False,
        )
        portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[working])
        portfolio_manager.trades = mocker.Mock()
        update_recovery = mocker.patch.object(
            portfolio_manager,
            "_update_tail_recovery_submission",
            return_value=True,
        )
        portfolio_manager.orders.add_order(
            contract,
            tail_order(portfolio_manager, "SELL", 1, "tg:tail-hedge:close"),
            None,
        )

        portfolio_manager.submit_orders()

        update_recovery.assert_not_called()
        portfolio_manager.trades.submit_order.assert_not_called()

    def test_submit_orders_requires_persisted_tail_sale_quantity(
        self, portfolio_manager, mocker
    ):
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        portfolio_manager.ibkr.portfolio = mocker.Mock(
            return_value=[position(portfolio_manager, contract, 2)]
        )
        portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
        portfolio_manager.trades = mocker.Mock()
        update_recovery = mocker.patch.object(
            portfolio_manager,
            "_update_tail_recovery_submission",
            return_value=False,
        )
        portfolio_manager.orders.add_order(
            contract,
            tail_order(portfolio_manager, "SELL", 2, "tg:tail-hedge:close"),
            None,
        )

        portfolio_manager.submit_orders()

        update_recovery.assert_called_once_with(123, 2, live_quantity=2)
        portfolio_manager.trades.submit_order.assert_not_called()

    def test_submit_orders_caps_tail_buy_to_live_short_position(
        self, portfolio_manager, mocker
    ):
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=789)
        portfolio_manager.ibkr.portfolio = mocker.Mock(
            return_value=[position(portfolio_manager, contract, -1)]
        )
        portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.submit_order.return_value = True
        portfolio_manager.orders.add_order(
            contract,
            tail_order(portfolio_manager, "BUY", 1, "tg:tail-harvest:SPY:789"),
            None,
        )
        portfolio_manager.orders.add_order(
            contract,
            tail_order(portfolio_manager, "BUY", 2, "tg:tail-hedge:close"),
            None,
        )

        portfolio_manager.submit_orders()

        portfolio_manager.trades.submit_order.assert_called_once()
        submitted_order = portfolio_manager.trades.submit_order.call_args.args[1]
        assert submitted_order.orderRef == "tg:tail-hedge:close"
        assert int(submitted_order.totalQuantity) == 1

    def test_submit_orders_requires_live_underlying_for_tail_entry(
        self, portfolio_manager, mocker
    ):
        portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
        portfolio_manager.ibkr.portfolio = mocker.Mock(
            return_value=[
                position(portfolio_manager, Stock("QQQ", "SMART"), 1),
                position(portfolio_manager, Stock("SPY", "SMART"), 1, "OTHER"),
            ]
        )
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.submit_order.return_value = True
        for symbol, con_id in [("QQQ", 100), ("SPY", 200)]:
            portfolio_manager.orders.add_order(
                Option(symbol, "20271217", 300, "P", "SMART", conId=con_id),
                tail_order(portfolio_manager, "BUY", 1, "tg:tail-hedge:entry"),
                None,
            )

        portfolio_manager.submit_orders()

        portfolio_manager.trades.submit_order.assert_called_once()
        assert portfolio_manager.trades.submit_order.call_args.args[0].symbol == "QQQ"

    def test_submit_orders_releases_tail_entry_without_live_underlying(
        self,
        portfolio_manager,
        mocker,
        tmp_path,
    ):
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        store = persist_tail_entry(portfolio_manager, tmp_path, contract)
        portfolio_manager.ibkr.portfolio = mocker.Mock(return_value=[])
        portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.orders.add_order(
            contract,
            tail_order(portfolio_manager, "BUY", 1, "tg:tail-hedge:entry"),
            None,
        )

        portfolio_manager.submit_orders()

        portfolio_manager.trades.submit_order.assert_not_called()
        assert store.load().cohorts == []

    def test_submit_orders_blocks_tail_entry_for_working_underlying_order(
        self, portfolio_manager, mocker
    ):
        active_stock_order = SimpleNamespace(
            contract=Stock("SPY", "SMART"),
            order=LimitOrder(
                "SELL",
                1,
                100.0,
                account=portfolio_manager.account_number,
            ),
            isDone=lambda: False,
        )
        other_account_order = SimpleNamespace(
            contract=Stock("QQQ", "SMART"),
            order=LimitOrder("SELL", 1, 100.0, account="OTHER"),
            isDone=lambda: False,
        )
        portfolio_manager.ibkr.open_trades = mocker.Mock(
            return_value=[active_stock_order, other_account_order]
        )
        portfolio_manager.ibkr.portfolio = mocker.Mock(
            return_value=[
                position(portfolio_manager, Stock("SPY", "SMART"), 1),
                position(portfolio_manager, Stock("QQQ", "SMART"), 1),
            ]
        )
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.submit_order.return_value = True
        for symbol, con_id in [("SPY", 100), ("QQQ", 200)]:
            portfolio_manager.orders.add_order(
                Option(symbol, "20271217", 300, "P", "SMART", conId=con_id),
                tail_order(
                    portfolio_manager,
                    "BUY",
                    1,
                    "tg:tail-hedge:entry",
                ),
                None,
            )

        portfolio_manager.submit_orders()

        portfolio_manager.trades.submit_order.assert_called_once()
        assert portfolio_manager.trades.submit_order.call_args.args[0].symbol == "QQQ"

    @pytest.mark.parametrize("occupancy", ["live", "working"])
    def test_submit_orders_blocks_occupied_tail_entry_contract(
        self,
        portfolio_manager,
        mocker,
        occupancy,
        tmp_path,
    ):
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        store = persist_tail_entry(portfolio_manager, tmp_path, contract)
        stock = position(portfolio_manager, Stock("SPY", "SMART"), 1)
        portfolio = [stock]
        open_trades = []
        if occupancy == "live":
            portfolio.append(position(portfolio_manager, contract, 1))
        else:
            open_trades.append(
                SimpleNamespace(
                    contract=contract,
                    order=LimitOrder(
                        "BUY",
                        1,
                        1.0,
                        account=portfolio_manager.account_number,
                        orderRef="wheel-entry",
                    ),
                    isDone=lambda: False,
                )
            )
        portfolio_manager.ibkr.portfolio = mocker.Mock(return_value=portfolio)
        portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=open_trades)
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.orders.add_order(
            contract,
            tail_order(portfolio_manager, "BUY", 1, "tg:tail-hedge:entry"),
            None,
        )

        portfolio_manager.submit_orders()

        portfolio_manager.trades.submit_order.assert_not_called()
        assert store.load().cohorts == []

    def test_submit_orders_preserves_matching_working_tail_entry_state(
        self,
        portfolio_manager,
        mocker,
        tmp_path,
    ):
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        store = persist_tail_entry(portfolio_manager, tmp_path, contract)
        portfolio_manager.ibkr.portfolio = mocker.Mock(
            return_value=[position(portfolio_manager, Stock("SPY", "SMART"), 1)]
        )
        working = SimpleNamespace(
            contract=contract,
            order=tail_order(
                portfolio_manager,
                "BUY",
                1,
                "tg:tail-hedge:entry",
            ),
            isDone=lambda: False,
        )
        portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[working])
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.orders.add_order(
            contract,
            tail_order(portfolio_manager, "BUY", 1, "tg:tail-hedge:entry"),
            None,
        )

        portfolio_manager.submit_orders()

        portfolio_manager.trades.submit_order.assert_not_called()
        assert len(store.load().cohorts) == 1

    def test_submit_orders_preserves_first_same_batch_tail_entry_state(
        self,
        portfolio_manager,
        mocker,
        tmp_path,
    ):
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        store = persist_tail_entry(portfolio_manager, tmp_path, contract)
        portfolio_manager.ibkr.portfolio = mocker.Mock(
            return_value=[position(portfolio_manager, Stock("SPY", "SMART"), 1)]
        )
        portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.submit_order.return_value = True
        order = tail_order(portfolio_manager, "BUY", 1, "tg:tail-hedge:entry")
        portfolio_manager.orders.add_order(contract, order, None)
        portfolio_manager.orders.add_order(contract, order, None)

        portfolio_manager.submit_orders()

        portfolio_manager.trades.submit_order.assert_called_once()
        remaining = store.load().cohorts
        assert len(remaining) == 1
        assert remaining[0].status == "entry_enqueued"

    @pytest.mark.parametrize("tail_first", [False, True])
    def test_submit_orders_blocks_tail_entry_for_same_run_underlying_order(
        self, portfolio_manager, mocker, tail_first
    ):
        portfolio_manager.ibkr.open_trades = mocker.Mock(return_value=[])
        portfolio_manager.ibkr.portfolio = mocker.Mock(
            return_value=[position(portfolio_manager, Stock("SPY", "SMART"), 1)]
        )
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.submit_order.return_value = True
        tail = (
            Option("SPY", "20271217", 300, "P", "SMART", conId=100),
            tail_order(
                portfolio_manager,
                "BUY",
                1,
                "tg:tail-hedge:entry",
            ),
            None,
        )
        stock = (
            Stock("SPY", "SMART"),
            tail_order(portfolio_manager, "SELL", 1, "cash-management"),
            None,
        )
        for contract, order, intent_id in (
            (tail, stock) if tail_first else (stock, tail)
        ):
            portfolio_manager.orders.add_order(contract, order, intent_id)

        portfolio_manager.submit_orders()

        portfolio_manager.trades.submit_order.assert_called_once()
        assert isinstance(
            portfolio_manager.trades.submit_order.call_args.args[0], Stock
        )

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
    async def test_manage_respects_disabled_run_stages(
        self, mock_ib, mock_config, mocker
    ):
        completion_future = mocker.Mock()
        pm = PortfolioManager(
            mock_config,
            mock_ib,
            completion_future,
            dry_run=True,
            run_stage_flags={
                "equity_regime_rebalance": False,
                "equity_buy_rebalance": False,
                "equity_sell_rebalance": False,
                "post_vix_call_hedge": False,
                "post_cash_management": False,
            },
        )

        pm.options_trading_enabled = mocker.Mock(return_value=False)
        pm.initialize_account = mocker.Mock()
        pm.summarize_account = mocker.AsyncMock(return_value=({}, {}))
        pm.get_portfolio_positions = mocker.Mock(return_value={})
        pm.equity_engine.check_regime_rebalance_positions = mocker.AsyncMock(
            return_value=(None, [])
        )
        pm.equity_engine.check_buy_only_positions = mocker.AsyncMock(
            return_value=(None, [])
        )
        pm.equity_engine.check_sell_only_positions = mocker.AsyncMock(
            return_value=(None, [])
        )
        pm.post_engine.do_vix_hedging = mocker.AsyncMock()
        pm.post_engine.do_cashman = mocker.AsyncMock()
        pm.orders.print_summary = mocker.Mock()

        await pm.manage()

        pm.equity_engine.check_regime_rebalance_positions.assert_not_called()
        pm.equity_engine.check_buy_only_positions.assert_not_called()
        pm.equity_engine.check_sell_only_positions.assert_not_called()
        pm.post_engine.do_vix_hedging.assert_not_called()
        pm.post_engine.do_cashman.assert_not_called()

    @pytest.mark.asyncio
    async def test_manage_executes_stages_in_explicit_run_order(
        self, mock_ib, mock_config, mocker
    ):
        completion_future = mocker.Mock()
        pm = PortfolioManager(
            mock_config,
            mock_ib,
            completion_future,
            dry_run=True,
            run_stage_order=[
                "equity_buy_rebalance",
                "options_write_puts",
                "post_cash_management",
            ],
        )

        pm.options_trading_enabled = mocker.Mock(return_value=True)
        pm.initialize_account = mocker.Mock()
        pm.summarize_account = mocker.AsyncMock(return_value=({}, {}))
        pm.get_portfolio_positions = mocker.Mock(return_value={})
        pm.orders.print_summary = mocker.Mock()

        calls: list[tuple[str, set[str]]] = []

        async def fake_run_option_write_stages(
            deps, _account_summary, _portfolio_positions, _options_enabled
        ):
            calls.append(("write", set(deps.enabled_stages)))

        async def fake_run_equity_rebalance_stages(
            deps, _account_summary, _portfolio_positions
        ):
            calls.append(("equity", set(deps.enabled_stages)))

        async def fake_run_post_stages(deps, _account_summary, _portfolio_positions):
            calls.append(("post", set(deps.enabled_stages)))

        mocker.patch(
            "thetagang.portfolio_manager.run_option_write_stages",
            side_effect=fake_run_option_write_stages,
        )
        mocker.patch(
            "thetagang.portfolio_manager.run_equity_rebalance_stages",
            side_effect=fake_run_equity_rebalance_stages,
        )
        mocker.patch(
            "thetagang.portfolio_manager.run_post_stages",
            side_effect=fake_run_post_stages,
        )
        mocker.patch("thetagang.portfolio_manager.run_option_management_stages")

        await pm.manage()

        assert calls == [
            ("equity", {"equity_buy_rebalance"}),
            ("write", {"options_write_puts"}),
            ("post", {"post_cash_management"}),
        ]
        pm.get_portfolio_positions.assert_called_once()

    @pytest.mark.asyncio
    async def test_manage_continues_if_order_submission_wait_times_out(
        self, mock_ib, mock_config, mocker
    ):
        completion_future = mocker.Mock()
        pm = PortfolioManager(
            mock_config,
            mock_ib,
            completion_future,
            dry_run=False,
            run_stage_order=["equity_buy_rebalance"],
        )

        pm.initialize_account = mocker.Mock()
        pm.summarize_account = mocker.AsyncMock(return_value=({}, {}))
        pm.get_portfolio_positions = mocker.Mock(return_value={})
        pm.orders.print_summary = mocker.Mock()
        pm.submit_orders = mocker.Mock()
        pm.adjust_prices = mocker.AsyncMock()
        pm.trades = mocker.Mock()
        pm.trades.records = mocker.Mock(return_value=[mocker.Mock()])

        mocker.patch(
            "thetagang.portfolio_manager.run_equity_rebalance_stages",
            new=mocker.AsyncMock(),
        )
        pm.ibkr.wait_for_submitting_orders = mocker.AsyncMock(
            side_effect=RuntimeError("timed out")
        )

        await pm.manage()

        pm.submit_orders.assert_called_once()
        assert pm.ibkr.wait_for_submitting_orders.await_count == 2
        pm.adjust_prices.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_manage_allows_incomplete_working_orders(
        self, mock_ib, mock_config, mocker
    ):
        completion_future = mocker.Mock()
        pm = PortfolioManager(
            mock_config,
            mock_ib,
            completion_future,
            dry_run=False,
            run_stage_order=["equity_buy_rebalance"],
        )

        pm.initialize_account = mocker.Mock()
        pm.summarize_account = mocker.AsyncMock(return_value=({}, {}))
        pm.get_portfolio_positions = mocker.Mock(return_value={})
        pm.orders.print_summary = mocker.Mock()
        pm.submit_orders = mocker.Mock()
        pm.adjust_prices = mocker.AsyncMock()

        trade = mocker.Mock()
        trade.contract = mocker.Mock(symbol="SPY")
        trade.order = mocker.Mock(orderId=123)
        trade.orderStatus = mocker.Mock(status="Submitted", filled=0.0, remaining=1.0)
        trade.isDone.return_value = False

        pm.trades = mocker.Mock()
        pm.trades.records = mocker.Mock(return_value=[trade])

        mocker.patch(
            "thetagang.portfolio_manager.run_equity_rebalance_stages",
            new=mocker.AsyncMock(),
        )
        pm.ibkr.wait_for_submitting_orders = mocker.AsyncMock(return_value=None)

        await pm.manage()

    @pytest.mark.asyncio
    async def test_tail_harvest_phase_reprices_then_fills(
        self, portfolio_manager, mocker
    ):
        portfolio_manager.config.runtime.orders.price_update_delay = [1, 2]
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        order = tail_order(
            portfolio_manager,
            "SELL",
            1,
            "tg:tail-harvest:SPY:123",
        )
        status = SimpleNamespace(status="Submitted", filled=0.5, remaining=0.5)
        trade = SimpleNamespace(
            contract=contract,
            order=order,
            orderStatus=status,
            isDone=lambda: status.status == "Filled",
        )
        trades = []
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.records.side_effect = lambda: trades
        portfolio_manager.submit_orders = mocker.Mock(
            side_effect=lambda _records: trades.append(trade)
        )
        portfolio_manager.ibkr.wait_for_submitting_orders = mocker.AsyncMock()
        portfolio_manager.ibkr.wait_for_orders_complete = mocker.AsyncMock(
            return_value=[trade]
        )
        portfolio_manager.ibkr.cancel_order = mocker.Mock()

        async def fill_on_reprice(_idx, _trade, **_kwargs):
            status.status = "Filled"
            status.filled = 1.0
            status.remaining = 0.0
            return True

        reprice = mocker.patch.object(
            portfolio_manager,
            "_reprice_trade",
            side_effect=fill_on_reprice,
        )
        filled = await portfolio_manager._execute_tail_harvest_phase(
            [(contract, order, None)],
            timeout=5,
        )

        assert filled is True
        reprice.assert_awaited_once()
        assert reprice.await_args.args == (0, trade)
        assert 0 < reprice.await_args.kwargs["timeout"] <= 5
        portfolio_manager.ibkr.cancel_order.assert_not_called()

    def test_tail_harvest_fill_check_accepts_normalized_filled_status(
        self, portfolio_manager
    ):
        trade = SimpleNamespace(
            order=SimpleNamespace(totalQuantity=1),
            orderStatus=SimpleNamespace(
                status="FILLED",
                filled=1,
                remaining=float("nan"),
            ),
        )

        assert portfolio_manager._trade_fully_filled(trade) is True

    @pytest.mark.parametrize(
        ("filled", "requested"),
        [(float("nan"), 1), (float("inf"), 1), (1, float("inf"))],
    )
    def test_tail_harvest_fill_check_rejects_non_finite_quantities(
        self,
        portfolio_manager,
        filled,
        requested,
    ):
        trade = SimpleNamespace(
            order=SimpleNamespace(totalQuantity=requested),
            orderStatus=SimpleNamespace(
                status="Filled",
                filled=filled,
                remaining=0,
            ),
        )

        assert portfolio_manager._trade_fully_filled(trade) is False

    @pytest.mark.asyncio
    async def test_tail_harvest_reprice_preserves_profit_floor(
        self, portfolio_manager, mocker
    ):
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        order = tail_order(
            portfolio_manager,
            "SELL",
            1,
            "tg:tail-harvest:SPY:123",
        )
        order.lmtPrice = 2.0
        setattr(order, TAIL_HEDGE_MIN_LIMIT_PRICE_ATTR, 1.5)
        trade = SimpleNamespace(contract=contract, order=order)
        ticker = mocker.Mock(spec=Ticker)
        ticker.midpoint.return_value = 0.5
        portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(
            return_value=ticker
        )
        portfolio_manager.trades = mocker.Mock()

        repriced = await portfolio_manager._reprice_trade(0, trade)

        assert repriced is True
        portfolio_manager.trades.submit_order.assert_called_once()
        submitted_order = portfolio_manager.trades.submit_order.call_args.args[1]
        assert submitted_order.lmtPrice == pytest.approx(1.5)

    @pytest.mark.asyncio
    async def test_tail_harvest_phase_cancels_and_fails_closed(
        self, portfolio_manager, mocker
    ):
        portfolio_manager.config.runtime.orders.price_update_delay = [1, 2]
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        order = tail_order(
            portfolio_manager,
            "SELL",
            1,
            "tg:tail-harvest:SPY:123",
        )
        status = SimpleNamespace(status="Submitted", filled=0.5, remaining=0.5)
        trade = SimpleNamespace(
            contract=contract,
            order=order,
            orderStatus=status,
            isDone=lambda: False,
        )
        trades = []
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.records.side_effect = lambda: trades
        portfolio_manager.submit_orders = mocker.Mock(
            side_effect=lambda _records: trades.append(trade)
        )
        portfolio_manager.ibkr.wait_for_submitting_orders = mocker.AsyncMock()
        portfolio_manager.ibkr.wait_for_orders_complete = mocker.AsyncMock(
            side_effect=[[trade], [trade], []]
        )
        portfolio_manager.ibkr.cancel_order = mocker.Mock()
        mocker.patch.object(
            portfolio_manager,
            "_reprice_trade",
            new=mocker.AsyncMock(return_value=True),
        )

        filled = await portfolio_manager._execute_tail_harvest_phase(
            [(contract, order, None)],
            timeout=1,
        )

        assert filled is False
        portfolio_manager.ibkr.cancel_order.assert_called_once_with(order)
        assert portfolio_manager.ibkr.wait_for_orders_complete.await_count == 3

    @pytest.mark.asyncio
    async def test_regime_stage_recalculates_after_filled_tail_harvest(
        self, portfolio_manager, mocker
    ):
        portfolio_manager.config.strategies.regime_rebalance.enabled = True
        portfolio_manager.data_store = mocker.Mock()
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        harvest_order = tail_order(
            portfolio_manager,
            "SELL",
            1,
            "tg:tail-harvest:SPY:123",
        )
        initial_summary = {"TotalCashValue": SimpleNamespace(value="0")}
        refreshed_summary = {
            "NetLiquidation": AccountValue(
                "TEST123", "NetLiquidation", "10000.0", "BASE", ""
            ),
            "TotalCashValue": AccountValue(
                "TEST123", "TotalCashValue", "2000.0", "BASE", ""
            ),
        }
        refreshed_positions = {"SPY": []}
        checks = 0

        async def check_regime(_summary, _positions, **_kwargs):
            nonlocal checks
            checks += 1
            if checks == 1:
                portfolio_manager.orders.add_order(contract, harvest_order, None)
                return mocker.Mock(), [("SPY", "NYSE", 1)]
            return mocker.Mock(), [("SPY", "NYSE", 2)]

        portfolio_manager.equity_engine.check_regime_rebalance_positions = (
            mocker.AsyncMock(side_effect=check_regime)
        )

        async def prepare_regime_orders(orders):
            for symbol, primary_exchange, quantity in orders:
                contract = Stock(
                    symbol,
                    "SMART",
                    "USD",
                    primaryExchange=primary_exchange,
                )
                order = LimitOrder(
                    "BUY" if quantity > 0 else "SELL",
                    abs(quantity),
                    100.0,
                    account="TEST123",
                    orderRef=f"tg:regime-rebalance:{symbol}",
                )
                portfolio_manager.orders.add_order(contract, order, None)
            return len(orders)

        execute = mocker.patch.object(
            portfolio_manager.equity_engine,
            "execute_regime_rebalance_orders",
            new=mocker.AsyncMock(side_effect=prepare_regime_orders),
        )
        execute_harvest = mocker.patch.object(
            portfolio_manager,
            "_execute_tail_harvest_phase",
            new=mocker.AsyncMock(return_value=True),
        )
        portfolio_manager.ibkr.refresh_account = mocker.AsyncMock()
        portfolio_manager.ibkr.account_summary = mocker.AsyncMock(return_value=[])
        portfolio_manager.ibkr.cached_account_value = mocker.Mock(
            side_effect=lambda _account, tag: {
                "NetLiquidation": 10000.0,
                "TotalCashValue": 2000.0,
            }[tag]
        )
        portfolio_manager.get_portfolio_positions = mocker.Mock(
            return_value=refreshed_positions
        )

        result = await portfolio_manager._run_regime_rebalance_stage(
            initial_summary,
            {"SPY": []},
        )

        assert result == (refreshed_summary, refreshed_positions)
        execute_harvest.assert_awaited_once_with([(contract, harvest_order, None)])
        portfolio_manager.data_store.discard_current_run_events.assert_called_once_with(
            {"regime_rebalance_state", "volatility_weight_state"}
        )
        portfolio_manager.ibkr.refresh_account.assert_awaited_once_with("TEST123")
        assert (
            portfolio_manager.equity_engine.check_regime_rebalance_positions.call_args_list[
                1
            ].kwargs["exclude_current_run_state"]
            is True
        )
        execute.assert_awaited_once_with([("SPY", "NYSE", 2)])
        assert len(portfolio_manager.orders.records()) == 1

    @pytest.mark.asyncio
    async def test_regime_stage_fails_if_post_harvest_buy_is_not_prepared(
        self, portfolio_manager, mocker
    ):
        portfolio_manager.config.strategies.regime_rebalance.enabled = True
        portfolio_manager.data_store = mocker.Mock()
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        harvest_order = tail_order(
            portfolio_manager,
            "SELL",
            1,
            "tg:tail-harvest:SPY:123",
        )
        checks = 0

        async def check_regime(_summary, _positions, **_kwargs):
            nonlocal checks
            checks += 1
            if checks == 1:
                portfolio_manager.orders.add_order(contract, harvest_order, None)
            return mocker.Mock(), [("SPY", "NYSE", 1)]

        portfolio_manager.equity_engine.check_regime_rebalance_positions = (
            mocker.AsyncMock(side_effect=check_regime)
        )

        portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(
            return_value=mocker.Mock()
        )
        mocker.patch.object(
            portfolio_manager.equity_engine,
            "_midpoint_or_market_price",
            return_value=float("nan"),
        )
        mocker.patch.object(
            portfolio_manager,
            "_execute_tail_harvest_phase",
            new=mocker.AsyncMock(return_value=True),
        )
        portfolio_manager.ibkr.refresh_account = mocker.AsyncMock()
        portfolio_manager.ibkr.account_summary = mocker.AsyncMock(return_value=[])
        portfolio_manager.ibkr.cached_account_value = mocker.Mock(return_value=10_000.0)
        portfolio_manager.get_portfolio_positions = mocker.Mock(
            return_value={"SPY": []}
        )

        with pytest.raises(RuntimeError, match="preparation was incomplete"):
            await portfolio_manager._run_regime_rebalance_stage(
                {"NetLiquidation": SimpleNamespace(value="10000")},
                {"SPY": []},
            )

        assert portfolio_manager.orders.records() == []
        assert portfolio_manager.data_store.discard_current_run_events.call_count == 2

    @pytest.mark.asyncio
    async def test_regime_stage_aborts_when_tail_harvest_does_not_fill(
        self, portfolio_manager, mocker
    ):
        portfolio_manager.config.strategies.regime_rebalance.enabled = True
        contract = Option("SPY", "20271217", 300, "P", "SMART", conId=123)
        harvest_order = tail_order(
            portfolio_manager,
            "SELL",
            1,
            "tg:tail-harvest:SPY:123",
        )

        async def check_regime(_summary, _positions, **_kwargs):
            portfolio_manager.orders.add_order(contract, harvest_order, None)
            return mocker.Mock(), [("SPY", "NYSE", 1)]

        portfolio_manager.equity_engine.check_regime_rebalance_positions = (
            mocker.AsyncMock(side_effect=check_regime)
        )
        execute = mocker.patch.object(
            portfolio_manager.equity_engine,
            "execute_regime_rebalance_orders",
            new=mocker.AsyncMock(),
        )
        mocker.patch.object(
            portfolio_manager,
            "_execute_tail_harvest_phase",
            new=mocker.AsyncMock(return_value=False),
        )

        with pytest.raises(RuntimeError, match="did not fully fill"):
            await portfolio_manager._run_regime_rebalance_stage(
                {"TotalCashValue": SimpleNamespace(value="0")},
                {"SPY": []},
            )

        execute.assert_not_awaited()
        assert portfolio_manager.orders.records() == []

    @pytest.mark.asyncio
    async def test_manage_does_not_submit_other_orders_after_harvest_abort(
        self, portfolio_manager, mocker
    ):
        portfolio_manager.run_stage_order = ["equity_regime_rebalance"]
        portfolio_manager.initialize_account = mocker.Mock()
        portfolio_manager.summarize_account = mocker.AsyncMock(return_value=({}, {}))
        portfolio_manager.options_trading_enabled = mocker.Mock(return_value=False)
        portfolio_manager._run_regime_rebalance_stage = mocker.AsyncMock(
            side_effect=RuntimeError("Tail-harvest execution did not fully fill")
        )
        portfolio_manager.submit_orders = mocker.Mock()

        with pytest.raises(RuntimeError, match="did not fully fill"):
            await portfolio_manager.manage()

        portfolio_manager.submit_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_adjust_prices_continues_if_midpoint_market_data_missing(
        self, portfolio_manager, mocker
    ):
        from thetagang.ibkr import RequiredFieldValidationError

        portfolio_manager.config.runtime.orders.price_update_delay = (1, 2)
        portfolio_manager.config.runtime.orders.minimum_credit = 0.01

        trade = mocker.Mock()
        trade.contract = mocker.Mock(symbol="SPY")
        trade.contract.symbol = "SPY"
        trade.order = mocker.Mock(lmtPrice=1.23, action="SELL", totalQuantity=1)
        trade.orderId = 101
        trade.contract.secType = "OPT"
        trade.orderStatus = mocker.Mock(status="Submitted", filled=0.0, remaining=1.0)
        trade.isDone.return_value = False

        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.records = mocker.Mock(return_value=[trade])
        portfolio_manager.trades.is_empty = mocker.Mock(return_value=False)

        portfolio_manager.config.portfolio.symbols = {
            "SPY": mocker.Mock(adjust_price_after_delay=True)
        }
        portfolio_manager.ibkr.wait_for_orders_complete = mocker.AsyncMock(
            return_value=[trade]
        )
        portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(
            side_effect=RequiredFieldValidationError("market data unavailable")
        )

        await portfolio_manager.adjust_prices()
        portfolio_manager.ibkr.get_ticker_for_contract.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_adjust_prices_continues_when_combo_bag_midpoint_times_out(
        self, portfolio_manager, mocker
    ):
        portfolio_manager.config.runtime.orders.price_update_delay = (1, 2)
        portfolio_manager.config.runtime.orders.minimum_credit = 0.01

        trade = mocker.Mock()
        trade.contract = mocker.Mock(symbol="QQQ")
        trade.contract.symbol = "QQQ"
        trade.contract.secType = "BAG"
        trade.order = mocker.Mock(lmtPrice=-1.25, action="BUY", totalQuantity=1)
        trade.orderStatus = mocker.Mock(status="Submitted", filled=0.0, remaining=1.0)
        trade.isDone.return_value = False

        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.records = mocker.Mock(return_value=[trade])
        portfolio_manager.trades.is_empty = mocker.Mock(return_value=False)

        portfolio_manager.config.portfolio.symbols = {
            "QQQ": mocker.Mock(adjust_price_after_delay=True)
        }
        portfolio_manager.ibkr.wait_for_orders_complete = mocker.AsyncMock(
            return_value=[trade]
        )
        portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(
            side_effect=TimeoutError()
        )

        await portfolio_manager.adjust_prices()

        portfolio_manager.ibkr.get_ticker_for_contract.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_adjust_prices_skips_tail_managed_orders(
        self, portfolio_manager, mocker
    ):
        portfolio_manager.config.runtime.orders.price_update_delay = (1, 2)
        portfolio_manager.config.portfolio.symbols = {
            "SPY": mocker.Mock(adjust_price_after_delay=True)
        }
        trade = mocker.Mock(
            contract=mocker.Mock(symbol="SPY", secType="OPT"),
            order=LimitOrder(
                "BUY",
                1,
                1.0,
                account=portfolio_manager.account_number,
                orderRef="tg:tail-harvest:SPY:123",
            ),
        )
        trade.isDone.return_value = False
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.is_empty.return_value = False
        portfolio_manager.trades.records.return_value = [trade]
        portfolio_manager.ibkr.wait_for_orders_complete = mocker.AsyncMock()
        portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock()

        await portfolio_manager.adjust_prices()

        portfolio_manager.ibkr.get_ticker_for_contract.assert_not_awaited()
        portfolio_manager.trades.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_adjust_prices_preserves_order_metadata(
        self, portfolio_manager, mocker
    ):
        portfolio_manager.config.runtime.orders.price_update_delay = (1, 2)
        portfolio_manager.config.runtime.orders.minimum_credit = 0.01
        portfolio_manager.config.portfolio.symbols = {
            "SPY": mocker.Mock(adjust_price_after_delay=True)
        }
        original_order = LimitOrder(
            "SELL",
            2,
            1.0,
            account=portfolio_manager.account_number,
            orderRef="tg:regime-rebalance:SPY",
            tif="GTC",
            transmit=False,
        )
        contract = mocker.Mock(symbol="SPY", secType="OPT")
        trade = mocker.Mock(contract=contract, order=original_order)
        trade.isDone.return_value = False
        portfolio_manager.trades = mocker.Mock()
        portfolio_manager.trades.is_empty.return_value = False
        portfolio_manager.trades.records.return_value = [trade]
        portfolio_manager.ibkr.wait_for_orders_complete = mocker.AsyncMock()
        ticker = mocker.Mock()
        ticker.midpoint.return_value = 0.8
        portfolio_manager.ibkr.get_ticker_for_contract = mocker.AsyncMock(
            return_value=ticker
        )

        await portfolio_manager.adjust_prices()

        portfolio_manager.trades.submit_order.assert_called_once()
        submitted_contract, submitted_order, submitted_idx = (
            portfolio_manager.trades.submit_order.call_args.args
        )
        assert submitted_contract is contract
        assert submitted_idx == 0
        assert submitted_order is not original_order
        assert submitted_order.lmtPrice == pytest.approx(0.9)
        assert (
            submitted_order.account,
            submitted_order.orderRef,
            submitted_order.tif,
            submitted_order.transmit,
        ) == (
            portfolio_manager.account_number,
            "tg:regime-rebalance:SPY",
            "GTC",
            False,
        )

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
        portfolio_manager.config.strategies.wheel.defaults.write_when.calls.green = (
            False  # Don't write when green
        )
        portfolio_manager.config.strategies.wheel.defaults.write_when.calls.red = True

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

    @pytest.mark.asyncio
    async def test_check_if_can_write_puts_skips_buy_only_symbols(
        self, portfolio_manager, mocker
    ):
        """Test that check_if_can_write_puts skips buy-only rebalancing symbols."""
        # Mock config
        portfolio_manager.config.portfolio.symbols = {
            "AAPL": mocker.Mock(weight=0.5, buy_only_rebalancing=True),
            "MSFT": mocker.Mock(weight=0.5, buy_only_rebalancing=False),
        }
        portfolio_manager.config.is_buy_only_rebalancing = mocker.Mock(
            side_effect=lambda s: s == "AAPL"
        )
        portfolio_manager.config.trading_is_allowed = mocker.Mock(return_value=True)
        portfolio_manager.config.can_write_when = mocker.Mock(return_value=(True, True))
        portfolio_manager.config.strategies.wheel.defaults.write_when = mocker.Mock()
        portfolio_manager.config.strategies.wheel.defaults.write_when.calculate_net_contracts = False

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
            _positions_table,
            _put_actions_table,
            to_write,
        ) = await portfolio_manager.options_engine.check_if_can_write_puts(
            account_summary, portfolio_positions
        )

        # Verify AAPL (buy-only) has 0 puts to write
        # Verify MSFT (normal) would have puts to write if conditions are met
        assert len(to_write) <= 1  # At most MSFT

        # If MSFT was added to to_write, verify it's not AAPL
        for symbol, _, _, _ in to_write:
            assert symbol != "AAPL"

    @pytest.mark.asyncio
    async def test_initial_portfolio_load_waits_for_synchronized_caches(
        self, portfolio_manager, mocker
    ):
        portfolio_manager.config.portfolio.symbols = {"AAPL": mocker.Mock()}

        portfolio_item = SimpleNamespace(
            account="TEST123",
            contract=SimpleNamespace(symbol="AAPL", conId=1),
            position=5,
        )
        snapshot_position = SimpleNamespace(
            account="TEST123",
            contract=SimpleNamespace(symbol="AAPL", conId=1),
            position=5,
        )

        portfolio_manager.ibkr.portfolio = mocker.Mock(
            side_effect=[[], [portfolio_item]]
        )
        portfolio_manager.ibkr.positions = mocker.Mock(return_value=[snapshot_position])
        sleep_mock = mocker.patch(
            "thetagang.portfolio_manager.asyncio.sleep", new=mocker.AsyncMock()
        )

        result = await portfolio_manager.load_initial_portfolio_positions()

        assert result == {"AAPL": [portfolio_item]}
        assert portfolio_manager.ibkr.positions.call_count == 2
        sleep_mock.assert_awaited_once_with(1)
        portfolio_manager.ibkr.ib.reqAccountUpdatesAsync.assert_not_called()
        portfolio_manager.ibkr.ib.reqPositionsAsync.assert_not_called()

    def test_get_portfolio_positions_rematerializes_live_cache_only(
        self, portfolio_manager, mocker
    ):
        portfolio_manager.config.portfolio.symbols = {"AAPL": mocker.Mock()}

        old_item = SimpleNamespace(
            account="TEST123",
            contract=SimpleNamespace(symbol="AAPL", conId=1),
            position=5,
        )
        new_item = SimpleNamespace(
            account="TEST123",
            contract=SimpleNamespace(symbol="AAPL", conId=1),
            position=3,
        )

        portfolio_manager.ibkr.portfolio = mocker.Mock(
            side_effect=[[old_item], [new_item]]
        )

        first = portfolio_manager.get_portfolio_positions()
        second = portfolio_manager.get_portfolio_positions()

        assert first == {"AAPL": [old_item]}
        assert second == {"AAPL": [new_item]}
        portfolio_manager.ibkr.ib.reqAccountUpdatesAsync.assert_not_called()
        portfolio_manager.ibkr.ib.reqPositionsAsync.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_buy_only_positions(self, portfolio_manager, mocker):
        """Test check_buy_only_positions method."""
        # Mock config
        portfolio_manager.config.portfolio.symbols = {
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
        (
            _buy_actions_table,
            to_buy,
        ) = await portfolio_manager.equity_engine.check_buy_only_positions(
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
        portfolio_manager.order_ops.get_order_exchange = mocker.Mock(
            return_value="SMART"
        )
        portfolio_manager.order_ops.get_algo_strategy = mocker.Mock(
            return_value="Adaptive"
        )
        portfolio_manager.order_ops.get_algo_params = mocker.Mock(return_value=[])
        portfolio_manager.order_ops.enqueue_order = mocker.Mock()
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
        mocker.patch(
            "thetagang.strategies.equity_engine.Stock", return_value=mock_stock
        )
        mock_order = mocker.Mock()
        portfolio_manager.order_ops.create_limit_order = mocker.Mock(
            return_value=mock_order
        )

        # Mock log.notice and log.error
        mocker.patch("thetagang.log.notice")
        mocker.patch("thetagang.log.error")

        # Test data
        buy_orders = [
            ("AAPL", "NASDAQ", 50),
            ("GOOGL", "NASDAQ", 30),
        ]

        # Execute
        await portfolio_manager.equity_engine.execute_buy_orders(buy_orders)

        # Verify orders were created
        assert portfolio_manager.order_ops.enqueue_order.call_count == 2

        # Verify order parameters
        portfolio_manager.order_ops.create_limit_order.assert_any_call(
            action="BUY",
            quantity=50,
            limit_price=150.0,
            transmit=True,
        )
        portfolio_manager.order_ops.create_limit_order.assert_any_call(
            action="BUY",
            quantity=30,
            limit_price=150.0,
            transmit=True,
        )

    @pytest.mark.asyncio
    async def test_buy_only_positions_insufficient_buying_power(
        self, portfolio_manager, mocker
    ):
        """Test check_buy_only_positions when there's insufficient buying power."""
        # Mock config
        portfolio_manager.config.portfolio.symbols = {
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
        (
            _buy_actions_table,
            to_buy,
        ) = await portfolio_manager.equity_engine.check_buy_only_positions(
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
        stock_buy_order.orderType = "LMT"
        stock_buy_order.lmtPrice = 150.0
        stock_buy_order.totalQuantity = 100

        option_sell_order = mocker.Mock()
        option_sell_order.action = "SELL"
        option_sell_order.orderType = "LMT"
        option_sell_order.lmtPrice = 2.50
        option_sell_order.totalQuantity = 5

        # Mock the orders.records() to return our test orders
        portfolio_manager.orders.records = mocker.Mock(
            return_value=[
                (mock_stock, stock_buy_order, None),
                (mock_option, option_sell_order, None),
            ]
        )

        # Calculate pending cash balance
        pending_balance = portfolio_manager.post_engine.calc_pending_cash_balance()

        # Expected:
        # Stock BUY: -150 * 100 * 1 = -15,000
        # Option SELL: 2.50 * 5 * 100 = 1,250
        assert pending_balance == -13750.0

    @pytest.mark.asyncio
    async def test_buy_only_minimum_shares_threshold(self, portfolio_manager, mocker):
        """Test that buy-only rebalancing respects minimum shares threshold."""
        # Mock config with minimum shares threshold
        portfolio_manager.config.portfolio.symbols = {
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
        (
            _buy_actions_table,
            to_buy,
        ) = await portfolio_manager.equity_engine.check_buy_only_positions(
            account_summary, portfolio_positions
        )

        # Target: 0.1 * 10000 = $1000, which is 6.66 shares
        # Since 6 shares < 10 minimum, should not buy
        assert len(to_buy) == 0

    @pytest.mark.asyncio
    async def test_buy_only_minimum_amount_threshold(self, portfolio_manager, mocker):
        """Test that buy-only rebalancing respects minimum dollar amount threshold."""
        # Mock config with minimum amount threshold
        portfolio_manager.config.portfolio.symbols = {
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
        (
            _buy_actions_table,
            to_buy,
        ) = await portfolio_manager.equity_engine.check_buy_only_positions(
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
        portfolio_manager.config.portfolio.symbols = {
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
        (
            _buy_actions_table,
            to_buy,
        ) = await portfolio_manager.equity_engine.check_buy_only_positions(
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
        portfolio_manager.config.portfolio.symbols = {
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
        (
            _buy_actions_table,
            to_buy,
        ) = await portfolio_manager.equity_engine.check_buy_only_positions(
            account_summary, portfolio_positions
        )

        # Target: 0.1 * 10000 = $1000, which is 6.66 shares (6 shares = $900)
        # Even though 6 shares meets min shares (1), $900 < $2000 min amount
        # Should not buy due to amount threshold
        assert len(to_buy) == 0
