import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import thetagang.db as db_module
from thetagang.db import (
    DataStore,
    Event,
    HistoricalBar,
    OrderIntent,
    OrderRecord,
    run_migrations,
    sqlite_db_path,
)


def test_data_store_records_executions_and_queries(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    data_store = DataStore(
        f"sqlite:///{db_path}",
        str(tmp_path / "thetagang.toml"),
        dry_run=False,
        config_text="test",
    )

    fills = [
        SimpleNamespace(
            execution=SimpleNamespace(
                execId="1",
                orderRef="tg:regime-rebalance:AAA",
                side="BOT",
                shares=1,
                price=100.0,
                time=datetime(2024, 1, 5, 12, 0, 0),
            ),
            contract=SimpleNamespace(symbol="AAA"),
            commissionReport=SimpleNamespace(commission=1.25),
            time=datetime(2024, 1, 5, 12, 0, 0),
        ),
        SimpleNamespace(
            execution=SimpleNamespace(
                execId="2",
                orderRef="tg:regime-rebalance:BBB",
                time=datetime(2024, 1, 7, 12, 0, 0),
            ),
            contract=SimpleNamespace(symbol="BBB"),
            time=datetime(2024, 1, 7, 12, 0, 0),
        ),
        SimpleNamespace(
            execution=SimpleNamespace(
                execId="3",
                orderRef="tg:other:CCC",
                time=datetime(2024, 1, 9, 12, 0, 0),
            ),
            contract=SimpleNamespace(symbol="CCC"),
            time=datetime(2024, 1, 9, 12, 0, 0),
        ),
    ]

    data_store.record_executions(fills)
    last = data_store.get_last_regime_rebalance_time(
        symbols=["AAA", "BBB"],
        order_ref_prefix="tg:regime-rebalance",
        start_time=datetime(2024, 1, 1, 0, 0, 0),
    )

    assert last == datetime(2024, 1, 7, 12, 0, 0)
    stored_fills = data_store.get_executions_for_order_refs(["tg:regime-rebalance:AAA"])
    assert len(stored_fills) == 1
    assert stored_fills[0].execution.orderRef == "tg:regime-rebalance:AAA"
    assert stored_fills[0].execution.side == "BOT"
    assert stored_fills[0].execution.shares == 1
    assert stored_fills[0].execution.price == 100.0
    assert stored_fills[0].commissionReport.commission == 1.25


def test_sqlite_db_path_parses(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    assert sqlite_db_path(f"sqlite:///{db_path}") == db_path
    assert sqlite_db_path("sqlite:///:memory:") is None
    assert sqlite_db_path("postgresql://localhost/db") is None


def test_run_migrations_restores_existing_db(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    sqlite3.connect(db_path).execute("create table t (id integer);").close()
    before = db_path.read_bytes()

    def _boom(*_args, **_kwargs) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(db_module, "_run_alembic_upgrade", _boom)

    try:
        run_migrations(f"sqlite:///{db_path}")
    except RuntimeError:
        pass

    after = db_path.read_bytes()
    assert before == after


def test_run_migrations_cleans_temp_on_failure(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    temp_path = Path(str(db_path) + ".tmp")

    def _boom(*_args, **_kwargs) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(db_module, "_run_alembic_upgrade", _boom)

    try:
        run_migrations(f"sqlite:///{db_path}")
    except RuntimeError:
        pass

    assert not db_path.exists()
    assert not temp_path.exists()


def test_record_historical_bars_upserts_and_parses_dates(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    data_store = DataStore(
        f"sqlite:///{db_path}",
        str(tmp_path / "thetagang.toml"),
        dry_run=False,
        config_text="test",
    )

    bars = [
        SimpleNamespace(
            date="20240105",
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10,
            barCount=1,
            average=1.2,
        )
    ]
    data_store.record_historical_bars("AAA", "1 day", bars)

    updated_bars = [
        SimpleNamespace(
            date="20240105",
            open=2.0,
            high=3.0,
            low=1.0,
            close=2.5,
            volume=20,
            barCount=2,
            average=2.2,
        )
    ]
    data_store.record_historical_bars("AAA", "1 day", updated_bars)

    with data_store.session_scope() as session:
        close, volume = session.execute(
            select(HistoricalBar.close, HistoricalBar.volume).where(
                HistoricalBar.symbol == "AAA"
            )
        ).one()

    assert close == 2.5
    assert volume == 20


def test_get_historical_bars_filters_by_symbol_timeframe_and_time(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    data_store = DataStore(
        f"sqlite:///{db_path}",
        str(tmp_path / "thetagang.toml"),
        dry_run=False,
        config_text="test",
    )

    data_store.record_historical_bars(
        "AAA",
        "1 day",
        [
            SimpleNamespace(date="20240104", close=1.0),
            SimpleNamespace(date="20240105", close=2.0),
        ],
    )
    data_store.record_historical_bars(
        "AAA",
        "1 hour",
        [SimpleNamespace(date="20240105 12:00:00", close=99.0)],
    )
    data_store.record_historical_bars(
        "BBB",
        "1 day",
        [SimpleNamespace(date="20240105", close=100.0)],
    )

    bars = data_store.get_historical_bars(
        "AAA",
        "1 day",
        datetime(2024, 1, 5, 0, 0, 0),
        datetime(2024, 1, 5, 23, 59, 59),
    )

    assert len(bars) == 1
    assert bars[0].date == datetime(2024, 1, 5)
    assert bars[0].close == 2.0


def test_record_executions_parses_string_times(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    data_store = DataStore(
        f"sqlite:///{db_path}",
        str(tmp_path / "thetagang.toml"),
        dry_run=False,
        config_text="test",
    )

    fills = [
        SimpleNamespace(
            execution=SimpleNamespace(
                execId="1",
                orderRef="tg:regime-rebalance:AAA",
                time="20240105 12:00:00",
            ),
            contract=SimpleNamespace(symbol="AAA"),
            time=None,
        )
    ]

    data_store.record_executions(fills)
    last = data_store.get_last_regime_rebalance_time(
        symbols=["AAA"],
        order_ref_prefix="tg:regime-rebalance",
        start_time=datetime(2024, 1, 1, 0, 0, 0),
    )

    assert last == datetime(2024, 1, 5, 12, 0, 0)


def test_record_order_intent_links_orders(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    data_store = DataStore(
        f"sqlite:///{db_path}",
        str(tmp_path / "thetagang.toml"),
        dry_run=True,
        config_text="test",
    )

    contract = SimpleNamespace(
        symbol="AAA",
        secType="STK",
        conId=101,
        exchange="SMART",
        currency="USD",
    )
    order = SimpleNamespace(
        action="BUY",
        totalQuantity=10,
        lmtPrice=123.45,
        orderType="LMT",
        orderRef="tg:test",
        tif="DAY",
    )

    intent_id = data_store.record_order_intent(contract, order)
    assert intent_id is not None
    data_store.record_order(contract, order, intent_id=intent_id)

    with data_store.session_scope() as session:
        intent_row = session.execute(
            select(OrderIntent.id, OrderIntent.dry_run).limit(1)
        ).one()
        record_intent_id = session.execute(
            select(OrderRecord.intent_id).limit(1)
        ).scalar_one()

    assert intent_row.id == intent_id
    assert intent_row.dry_run is True
    assert record_intent_id == intent_id


def test_get_last_event_payload_ignores_dry_run(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    config_path = str(tmp_path / "thetagang.toml")

    dry_run_store = DataStore(
        f"sqlite:///{db_path}",
        config_path,
        dry_run=True,
        config_text="test",
    )
    live_store = DataStore(
        f"sqlite:///{db_path}",
        config_path,
        dry_run=False,
        config_text="test",
    )

    assert dry_run_store.record_event("regime_rebalance_state", {"flow_active": True})
    assert live_store.record_event("regime_rebalance_state", {"flow_active": False})

    payload = live_store.get_last_event_payload("regime_rebalance_state")

    assert payload == {"flow_active": False}


def test_get_last_event_payload_breaks_timestamp_ties_by_event_id(tmp_path) -> None:
    data_store = DataStore(
        f"sqlite:///{tmp_path / 'state.db'}",
        str(tmp_path / "thetagang.toml"),
        dry_run=False,
        config_text="test",
    )
    created_at = datetime(2026, 8, 13, 12, 0, 0)
    with data_store.session_scope() as session:
        session.add_all(
            [
                Event(
                    run_id=data_store.run_id,
                    created_at=created_at,
                    event_type="tail_hedge_state",
                    payload='{"sequence": 1}',
                ),
                Event(
                    run_id=data_store.run_id,
                    created_at=created_at,
                    event_type="tail_hedge_state",
                    payload='{"sequence": 2}',
                ),
            ]
        )

    assert data_store.get_last_event_payload("tail_hedge_state") == {"sequence": 2}


def test_get_last_event_payload_rejects_non_object_json(tmp_path) -> None:
    data_store = DataStore(
        f"sqlite:///{tmp_path / 'state.db'}",
        str(tmp_path / "thetagang.toml"),
        dry_run=False,
        config_text="test",
    )
    with data_store.session_scope() as session:
        session.add(
            Event(
                run_id=data_store.run_id,
                event_type="tail_hedge_state",
                payload="[]",
            )
        )

    assert data_store.get_last_event_payload("tail_hedge_state") is None
    with pytest.raises(RuntimeError, match="Failed to read event tail_hedge_state"):
        data_store.get_last_event_payload(
            "tail_hedge_state",
            raise_on_error=True,
        )


def test_record_event_reports_persistence_failure(tmp_path, monkeypatch) -> None:
    data_store = DataStore(
        f"sqlite:///{tmp_path / 'state.db'}",
        str(tmp_path / "thetagang.toml"),
        dry_run=False,
        config_text="test",
    )

    @contextmanager
    def failing_session_scope():
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(data_store, "session_scope", failing_session_scope)

    assert not data_store.record_event("required_state", {"value": 1})


def test_get_last_event_payload_can_fail_closed(tmp_path, monkeypatch) -> None:
    data_store = DataStore(
        f"sqlite:///{tmp_path / 'state.db'}",
        str(tmp_path / "thetagang.toml"),
        dry_run=False,
        config_text="test",
    )

    @contextmanager
    def failing_session_scope():
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(data_store, "session_scope", failing_session_scope)

    assert data_store.get_last_event_payload("best_effort_state") is None
    with pytest.raises(RuntimeError, match="Failed to read event required_state"):
        data_store.get_last_event_payload("required_state", raise_on_error=True)
