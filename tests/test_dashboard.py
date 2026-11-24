from __future__ import annotations

import io
import json
import sqlite3
from http.server import ThreadingHTTPServer
from pathlib import Path
from socket import socket
from types import SimpleNamespace
from typing import Iterator, cast
from unittest.mock import Mock

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine

from thetagang.dashboard import DashboardStore, cli, make_handler, resolve_database
from thetagang.db import Base, DataStore
from thetagang.trading_operations import OrderOperations


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection]]:
    path = tmp_path / "records.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    conn = sqlite3.connect(path)
    config = """[runtime.account]
number="private-account"
margin_usage=0.5
[runtime.ibc]
password="DO-NOT-EXPOSE"
[portfolio.symbols.SPY]
weight=1.0
"""
    for run_id, dry_run in [(1, 0), (2, 0), (3, 1)]:
        conn.execute(
            "INSERT INTO runs VALUES (?, ?, 'config.toml', ?, '2.0.3', 'private-host', ?)",
            (run_id, f"2026-09-11 14:0{run_id}:00", dry_run, config),
        )
    for run_id, ref in [(1, "tg:order:first"), (2, "tg:order:second")]:
        conn.execute(
            """INSERT INTO order_intents (id, run_id, created_at, dry_run, symbol,
               quantity, action, limit_price, order_ref, sec_type, payload_json)
               VALUES (?, ?, '2026-09-11 14:05:00', 0, 'SPY', 2, 'SELL', 3.5, ?, 'OPT', ?)""",
            (
                run_id,
                run_id,
                ref,
                json.dumps(
                    {
                        "contract": {
                            "strike": 100,
                            "right": "P",
                            "lastTradeDateOrContractMonth": "20261030",
                        },
                        "order": {"account": "private-account"},
                    }
                ),
            ),
        )
        conn.execute(
            """INSERT INTO orders (id,run_id,created_at,symbol,quantity,action,
               limit_price,order_id,intent_id,order_ref,sec_type)
               VALUES (?,?,'2026-09-11 14:06:00','SPY',2,'SELL',3.5,99,?,?,'OPT')""",
            (run_id, run_id, run_id, ref),
        )
    conn.execute("""INSERT INTO order_statuses (run_id,created_at,order_id,status,filled,remaining,perm_id)
                    VALUES (1,'2026-09-11 14:07:00',99,'Submitted',0,2,111)""")
    conn.execute("""INSERT INTO order_statuses (run_id,created_at,order_id,status,filled,remaining,perm_id)
                    VALUES (2,'2026-09-11 14:08:00',99,'Filled',2,0,222)""")
    conn.execute(
        """INSERT INTO events (run_id,created_at,event_type,payload)
                    VALUES (1,'2026-09-11 14:08:00','run_end',?)""",
        (json.dumps({"status": "working", "success": True}),),
    )
    conn.commit()
    yield path, conn
    conn.close()


def test_read_only_history_and_credentials(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    path, _ = database
    before = path.read_bytes()
    store = DashboardStore(path)
    history = store.overview()
    detail = store.detail(1)
    assert history["total"] == 3
    assert store.overview(mode="preview")["runs"][0]["mode"] == "预演"
    assert store.overview(mode="trade")["total"] == 2
    assert detail["run"]["status"] == "working"
    assert detail["settings"]["weights"] == {"SPY": 1.0}
    assert detail["run"]["started_at"] == "2026-09-11T14:01:00+00:00"
    text = json.dumps(detail)
    for private in ["DO-NOT-EXPOSE", "private-account", "private-host", "config_text"]:
        assert private not in text
    assert path.read_bytes() == before
    with store.read() as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM runs")


def test_reused_api_order_ids_do_not_merge(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    store = DashboardStore(database[0])
    assert store.detail(1)["orders"][0]["status"] == "Submitted"
    assert store.detail(1)["orders"][0]["filled"] == 0
    assert store.detail(2)["orders"][0]["status"] == "Filled"


def test_later_permanent_id_observation_and_execution_ref(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    path, conn = database
    conn.execute("""INSERT INTO order_statuses (run_id,created_at,order_id,status,filled,remaining,perm_id)
                    VALUES (2,'2026-09-11 14:10:00',501,'Cancelled',1,1,111)""")
    conn.execute("""INSERT INTO executions (run_id,created_at,exec_id,order_id,order_ref,symbol,side,shares,price,execution_time)
                    VALUES (2,'2026-09-11 14:10:00','fill1',501,'tg:order:first','SPY','SLD',1,3.5,'2026-09-11 14:09:00')""")
    conn.commit()
    store = DashboardStore(path)
    order = store.detail(1)["orders"][0]
    assert order["status"] == "Cancelled" and order["partial"]
    assert len(order["timeline"]) == 2
    assert len(order["executions"]) == 1
    assert not store.detail(1)["observed_executions"]
    assert len(store.detail(2)["observed_executions"]) == 1


def test_ambiguous_client_order_id_is_unknown(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    path, conn = database
    conn.execute("""INSERT INTO order_statuses (run_id,created_at,order_id,status,filled,remaining,perm_id)
                    VALUES (1,'2026-09-11 14:09:00',99,'Filled',2,0,333)""")
    conn.commit()
    assert DashboardStore(path).detail(1)["orders"][0]["status"] == "unknown"


@pytest.mark.parametrize("intent_id", [None, 999])
def test_legacy_submission_without_matching_intent_is_retained(
    database: tuple[Path, sqlite3.Connection], intent_id: int | None
) -> None:
    path, conn = database
    conn.execute(
        "UPDATE orders SET intent_id=?, order_ref=NULL WHERE run_id=1", (intent_id,)
    )
    conn.commit()
    orders = DashboardStore(path).detail(1)["orders"]
    planned = next(o for o in orders if o["order_ref"])
    submitted = next(o for o in orders if not o["order_ref"])
    assert planned["status"] == "intent" and planned["submitted_at"] is None
    assert submitted["order_id"] == 99 and submitted["status"] == "Submitted"
    assert submitted["planned_at"] is None
    assert submitted["submitted_at"] == "2026-09-11T14:06:00+00:00"


def test_missing_terminal_and_legacy_decisions_are_not_invented(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    detail = DashboardStore(database[0]).detail(3)
    assert detail["run"]["status"] == "unrecorded"
    assert detail["run"]["ended_at"] is None
    assert detail["decisions"] == []
    assert detail["account"]["NetLiquidation"]["value"] is None


def test_latest_position_snapshot_not_summed(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    path, conn = database
    for time, shares in [("14:01:00", 100), ("14:05:00", 200)]:
        conn.execute(
            """INSERT INTO position_snapshots (run_id, created_at, symbol, position)
                        VALUES (1, ?, 'SPY', ?)""",
            ("2026-09-11 " + time, shares),
        )
    conn.execute(
        """INSERT INTO account_snapshots (run_id,created_at,summary_json)
                    VALUES (1,'2026-09-11 14:01:00',?)""",
        (json.dumps({"NetLiquidation": {"value": "NaN"}}),),
    )
    conn.commit()
    detail = DashboardStore(path).detail(1)
    assert [p["position"] for p in detail["positions"]] == [200]
    assert detail["account"]["NetLiquidation"]["value"] is None
    json.dumps(detail, allow_nan=False)


def test_missing_file_never_created(tmp_path: Path) -> None:
    path = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        DashboardStore(path).overview()
    assert not path.exists()


def test_missing_optional_tables_and_empty_database(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    path, conn = database
    conn.execute("DROP TABLE executions")
    conn.commit()
    assert DashboardStore(path).detail(1)["observed_executions"] == []
    with pytest.raises(LookupError):
        DashboardStore(path).detail(99)
    conn.execute("DELETE FROM runs")
    conn.commit()
    assert DashboardStore(path).overview()["total"] == 0


def test_config_relative_path_without_trading_imports(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[runtime.database]\nenabled=true\npath="records/history.db"', encoding="utf-8"
    )
    assert resolve_database(config) == tmp_path / "records/history.db"
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0 and "不连接券商" in result.output


def response(
    store: DashboardStore, path: str, headers: str = "", method: str = "GET"
) -> bytes:
    class Connection:
        def __init__(self) -> None:
            self.output = bytearray()

        def makefile(self, *_args: object) -> io.BytesIO:
            return io.BytesIO(f"{method} {path} HTTP/1.0\r\n{headers}\r\n".encode())

        def sendall(self, data: bytes) -> None:
            self.output.extend(data)

    connection = Connection()
    make_handler(store)(
        cast(socket, connection),
        ("127.0.0.1", 1000),
        cast(ThreadingHTTPServer, SimpleNamespace(server_port=8765)),
    )
    return bytes(connection.output)


@pytest.mark.parametrize(
    "headers",
    [
        "Host: attacker.test\r\n",
        "Host: 127.0.0.1:8765\r\nOrigin: https://attacker.test\r\n",
    ],
)
def test_cross_origin_blocked(
    database: tuple[Path, sqlite3.Connection], headers: str
) -> None:
    result = response(DashboardStore(database[0]), "/api/runs", headers)
    assert b"403" in result.splitlines()[0]


@pytest.mark.parametrize(
    "path, status",
    [
        ("/api/runs", 200),
        ("/api/runs/1", 200),
        ("/api/runs/1/export", 200),
        ("/api/runs/999", 404),
        ("/api/runs?page=bad", 400),
        ("/../thetagang.toml", 404),
        ("/", 200),
        ("/app.js", 200),
    ],
)
def test_http_routes(
    database: tuple[Path, sqlite3.Connection], path: str, status: int
) -> None:
    result = response(DashboardStore(database[0]), path, "Host: 127.0.0.1:8765\r\n")
    assert str(status).encode() in result.splitlines()[0]
    assert b"Cache-Control: no-store" in result
    if path.endswith("export"):
        assert b"Content-Disposition: attachment" in result
        data = json.loads(result.split(b"\r\n\r\n", 1)[1])
        assert data["run"]["id"] == 1
        assert b"DO-NOT-EXPOSE" not in result


def test_no_write_routes(database: tuple[Path, sqlite3.Connection]) -> None:
    result = response(
        DashboardStore(database[0]), "/api/runs/1", "Host: 127.0.0.1:8765\r\n", "POST"
    )
    assert b"501" in result.splitlines()[0]


def test_decision_trace_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "decisions.db"
    store = DataStore(f"sqlite:///{path}", "config.toml", True)
    try:
        ops = OrderOperations(
            config=Mock(), account_number="test", orders=Mock(), data_store=store
        )
        ops.record_decision(
            "QQQ",
            "put_write",
            "skipped",
            "日内变动未达到门槛",
            threshold=7.16,
            daily_change=0,
        )
        store.record_event(
            "run_end", {"status": "failed", "error_type": "TimeoutError"}
        )
        detail = DashboardStore(path).detail(store.run_id)
        assert detail["decisions"][0]["reason"] == "日内变动未达到门槛"
        assert detail["decisions"][0]["metrics"]["threshold"] == 7.16
        assert detail["run"]["error_type"] == "TimeoutError"
        assert store.run_ended
    finally:
        store.engine.dispose()


@pytest.mark.parametrize("failure", [False, True])
def test_startup_exit_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    import tomlkit

    import thetagang.thetagang as runner
    from tests.test_thetagang_watchdog import _write_config

    config_path = _write_config(tmp_path)
    config_text = tomlkit.parse(config_path.read_text()).unwrap()
    config_text["runtime"]["database"] = {"enabled": True, "path": "history.db"}
    config_path.write_text(tomlkit.dumps(config_text), encoding="utf-8")
    monkeypatch.setattr(runner, "need_to_exit", lambda _: True)
    if failure:
        monkeypatch.setattr(
            runner, "need_to_exit", Mock(side_effect=TimeoutError("test"))
        )
        with pytest.raises(TimeoutError):
            runner.start(str(config_path), without_ibc=True)
    else:
        runner.start(str(config_path), without_ibc=True)
    detail = DashboardStore(tmp_path / "history.db").detail(1)
    assert detail["run"]["status"] == ("failed" if failure else "skipped")
    assert detail["run"]["ended_at"] is not None
    if failure:
        assert detail["run"]["error_type"] == "TimeoutError"
