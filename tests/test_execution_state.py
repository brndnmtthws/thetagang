import asyncio
import json
import subprocess
import sys
from copy import copy
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from ib_async import (
    CommissionReport,
    Contract,
    Execution,
    Fill,
    LimitOrder,
    OrderStatus,
    Stock,
    Trade,
)

from thetagang.execution import ExecutionError, OrderSubmissionError
from thetagang.execution_state import ExecutionJournal, account_lock, account_state_path
from thetagang.ibkr import IBKR, DryRunOrderError
from thetagang.trades import Trades


@pytest.fixture
def journal(tmp_path):
    return ExecutionJournal(tmp_path / "account", "DU-TEST")


def order():
    return LimitOrder(
        "BUY",
        10,
        100,
        account="DU-TEST",
        orderRef="tg:test:unique",
        orderId=7,
        clientId=3,
    )


def fill(contract, *, quantity=10, execution_id="exec-1", account="DU-TEST"):
    return Fill(
        contract,
        Execution(
            execId=execution_id,
            acctNumber=account,
            orderRef="tg:test:unique",
            shares=quantity,
        ),
        CommissionReport(),
        datetime.now(timezone.utc),
    )


def test_account_lock_excludes_other_process_and_releases_on_error(tmp_path):
    path = account_state_path(str(tmp_path), "DU-TEST")
    script = "from pathlib import Path; from thetagang.execution_state import account_lock;\nwith account_lock(Path(__import__('sys').argv[1])): pass"
    with pytest.raises(ValueError):
        with account_lock(path):
            competing = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                capture_output=True,
                text=True,
            )
            assert competing.returncode != 0
            assert "execution lock" in competing.stderr
            raise ValueError("simulated crash")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    assert "DU-TEST" not in str(path)
    assert path != account_state_path(str(tmp_path), "DU-OTHER")


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"version":2}',
        '{"version":1,"account":"DU-OTHER","submissions":{}}',
    ],
)
def test_corrupt_or_wrong_account_journal_fails_closed(tmp_path, content):
    path = tmp_path / "account.json"
    path.write_text(content)
    with pytest.raises(ExecutionError, match="Cannot trust"):
        ExecutionJournal(path, "DU-TEST")


def test_submission_is_durable_before_broker_call_and_unknown_survives_restart(journal):
    adapter = Mock()
    contract = Stock("SPY", "SMART", "USD")

    def fail_after_send(*_):
        content = json.loads(journal.path.read_text())
        assert content["submissions"][order().orderRef]["state"] == "submitting"
        raise ConnectionError("lost after send")

    adapter.place_order.side_effect = fail_after_send
    trades = Trades(adapter, journal=journal)
    with pytest.raises(OrderSubmissionError):
        trades.submit_order(contract, order())
    restarted = ExecutionJournal(journal.path, "DU-TEST")
    with pytest.raises(ExecutionError, match="Unresolved"):
        restarted.reconcile([], [])
    with pytest.raises(ExecutionError, match="already been"):
        Trades(adapter, journal=restarted).submit_order(contract, order())
    assert adapter.place_order.call_count == 1


def test_persistence_failure_prevents_any_broker_submission(journal, monkeypatch):
    adapter = Mock()
    monkeypatch.setattr(
        "thetagang.execution_state.os.replace", Mock(side_effect=OSError("disk full"))
    )
    with pytest.raises(ExecutionError, match="persisted"):
        Trades(adapter, journal=journal).submit_order(
            Stock("SPY", "SMART", "USD"), order()
        )
    adapter.place_order.assert_not_called()


@pytest.mark.parametrize(
    "status,unresolved",
    [
        ("Submitted", True),
        ("PendingSubmit", True),
        ("PendingCancel", True),
        ("Inactive", True),
        ("Filled", False),
        ("Cancelled", False),
        ("ApiCancelled", False),
    ],
)
def test_reconcile_requires_terminal_broker_evidence(journal, status, unresolved):
    contract = Stock("SPY", "SMART", "USD")
    submitted = order()
    journal.begin(contract, submitted, amendment=False)
    trade = Trade(contract, submitted, OrderStatus(status=status))
    if unresolved:
        with pytest.raises(ExecutionError, match="Unresolved"):
            journal.reconcile([trade], [])
    else:
        journal.reconcile([trade], [])
        assert journal.data.submissions[submitted.orderRef].state == "terminal"


def test_partial_duplicate_and_foreign_fills_do_not_clear_journal(journal):
    contract = Stock("SPY", "SMART", "USD")
    journal.begin(contract, order(), amendment=False)
    partial = fill(contract, quantity=5)
    with pytest.raises(ExecutionError):
        journal.reconcile([], [partial, partial, fill(contract, account="DU-OTHER")])
    journal.reconcile([], [partial, fill(contract, quantity=5, execution_id="exec-2")])
    assert journal.data.submissions[order().orderRef].status == "Filled"


def test_combo_leg_fills_cannot_be_counted_as_full_combo_execution(journal):
    journal.begin(
        Contract(secType="BAG", symbol="SPY", currency="USD"), order(), amendment=False
    )
    with pytest.raises(ExecutionError):
        journal.reconcile(
            [],
            [
                fill(Contract(secType="OPT", conId=1)),
                fill(Contract(secType="OPT", conId=2), execution_id="leg2"),
            ],
        )


def test_execution_corrections_are_not_added_as_extra_fills(journal):
    contract = Stock("SPY", "SMART", "USD")
    journal.begin(contract, order(), amendment=False)
    correction = fill(contract, quantity=4, execution_id="trade.02")
    original = fill(contract, quantity=6, execution_id="trade.01")
    with pytest.raises(ExecutionError):
        journal.reconcile([], [correction, original])
    journal.reconcile(
        [], [original, correction, fill(contract, quantity=6, execution_id="other.01")]
    )


def test_amendment_requires_acknowledged_matching_order(journal):
    contract, submitted = Stock("SPY", "SMART", "USD"), order()
    journal.begin(contract, submitted, amendment=False)
    with pytest.raises(ExecutionError, match="Cannot amend"):
        journal.begin(contract, submitted, amendment=True)
    journal.observe(Trade(contract, submitted, OrderStatus(status="Submitted")))
    different = copy(submitted)
    different.orderId += 1
    with pytest.raises(ExecutionError, match="Cannot amend"):
        journal.begin(contract, different, amendment=True)
    journal.begin(contract, submitted, amendment=True)


@pytest.mark.asyncio
async def test_repeated_manage_event_runs_strategy_once(manager, mocker):
    manager.completion_future = asyncio.get_running_loop().create_future()
    mocker.patch.object(manager, "initialize_account")
    summary = mocker.patch.object(manager, "summarize_account", return_value=({}, {}))
    mocker.patch.object(manager, "options_trading_enabled", return_value=False)
    mocker.patch.object(manager, "adjust_prices")
    mocker.patch.object(manager.ibkr, "wait_for_submitting_orders")
    await asyncio.gather(manager.manage(), manager.manage())
    summary.assert_awaited_once()
    assert await manager.completion_future is True


@pytest.mark.asyncio
async def test_startup_reconciliation_blocks_before_planning(
    manager, broker, journal, mocker
):
    manager.execution_journal = journal
    manager.config.runtime.account.cancel_orders = False
    manager.completion_future = asyncio.get_running_loop().create_future()
    journal.begin(Stock("SPY", "SMART", "USD"), order(), amendment=False)
    broker.reqExecutionsAsync.return_value = []
    summary = mocker.patch.object(manager, "summarize_account")
    with pytest.raises(ExecutionError, match="Unresolved"):
        await manager.manage()
    assert isinstance(manager.completion_future.exception(), ExecutionError)
    summary.assert_not_called()
    broker.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_unconfirmed_startup_cancel_blocks_replanning(
    manager, broker, journal, mocker
):
    manager.execution_journal = journal
    working = Trade(
        Stock("SPY", "SMART", "USD"), order(), OrderStatus(status="Submitted")
    )
    broker.reqAllOpenOrdersAsync.return_value = [working]
    mocker.patch.object(manager.ibkr, "wait_for_orders_complete")
    with pytest.raises(ExecutionError, match="cancellation was not confirmed"):
        await manager.reconcile_before_run()
    broker.cancelOrder.assert_called_once()
    broker.placeOrder.assert_not_called()


def test_dry_run_bypass_does_not_create_a_pending_journal_entry(broker, journal):
    trades = Trades(IBKR(broker, 1, "SMART", dry_run=True), journal=journal)
    with pytest.raises(DryRunOrderError):
        trades.submit_order(Stock("SPY", "SMART", "USD"), order())
    assert not journal.path.exists()
    assert not journal.data.submissions
    broker.placeOrder.assert_not_called()


def test_async_acknowledgement_allows_amendment_with_same_identity(broker, journal):
    adapter = IBKR(broker, 1, "SMART")
    trades = Trades(adapter, journal=journal)
    contract, submitted = Stock("SPY", "SMART", "USD"), order()
    trade = Trade(contract, submitted, OrderStatus(status="PendingSubmit"))
    broker.placeOrder.return_value = trade
    trades.submit_order(contract, submitted)
    trade.orderStatus.status = "Submitted"
    amendment = copy(submitted)
    amendment.lmtPrice = 101
    trades.submit_order(contract, amendment, idx=0)
    assert broker.placeOrder.call_count == 2
    assert len(journal.data.submissions) == 1
