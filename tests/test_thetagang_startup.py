import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomlkit

import thetagang.thetagang as tg
from tests.test_thetagang_watchdog import DummyEvent, _write_config
from thetagang.execution import OrderSubmissionError


def test_configure_ib_async_logging_noop_when_empty(monkeypatch):
    called = {"log": False}

    def fake_log_to_file(_path: str) -> None:
        called["log"] = True

    monkeypatch.setattr(tg.util, "logToFile", fake_log_to_file)

    tg._configure_ib_async_logging("")

    assert called["log"] is False


def test_configure_ib_async_logging_creates_parent_and_configures(
    monkeypatch, tmp_path
):
    target = tmp_path / "nested" / "logs" / "ib.log"
    called = {"path": None}

    def fake_log_to_file(path: str) -> None:
        called["path"] = path

    monkeypatch.setattr(tg.util, "logToFile", fake_log_to_file)

    tg._configure_ib_async_logging(str(target))

    assert (tmp_path / "nested" / "logs").is_dir()
    assert called["path"] == str(target)


def test_configure_ib_async_logging_warns_and_continues_on_oserror(
    monkeypatch, tmp_path
):
    target = tmp_path / "logs" / "ib.log"
    warnings: list[str] = []

    def fake_log_to_file(_path: str) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(tg.util, "logToFile", fake_log_to_file)
    monkeypatch.setattr(tg.log, "warning", lambda message: warnings.append(message))

    tg._configure_ib_async_logging(str(target))

    assert len(warnings) == 1
    assert "Unable to initialize ib_async logfile" in warnings[0]
    assert str(Path(target)) in warnings[0]


def test_disconnect_stops_active_run_without_restart_and_releases_lock(
    monkeypatch, tmp_path
):
    config_path = _write_config(tmp_path)
    loop = asyncio.new_event_loop()
    captured = {"manage_calls": 0, "cancelled": False, "disconnects": 0}
    monkeypatch.setattr(tg.util, "getLoop", lambda: loop)
    monkeypatch.setattr(tg, "need_to_exit", lambda *_: False)

    class FakeIB:
        def __init__(self):
            self.connectedEvent = DummyEvent()
            self.disconnectedEvent = DummyEvent()
            self.client = SimpleNamespace(serverVersion=lambda: 100)

        def connect(self, *_args, **_kwargs):
            for handler in self.connectedEvent._handlers:
                loop.create_task(handler())

        def run(self, awaitable):
            return loop.run_until_complete(awaitable)

        def disconnect(self):
            captured["disconnects"] += 1
            self.disconnectedEvent.emit()

    class FakeManager:
        def __init__(self, _config, ib, _future, _dry_run, **_kwargs):
            self.ib = ib
            self.connection_lost = False

        async def manage(self):
            captured["manage_calls"] += 1
            # A network disconnect and a repeated connected event must not start
            # another strategy run in this process.
            loop.call_soon(self.ib.disconnectedEvent.emit)
            loop.call_soon(self.ib.connect)
            try:
                await asyncio.Event().wait()
            finally:
                captured["cancelled"] = True

    monkeypatch.setattr(tg, "IB", FakeIB)
    monkeypatch.setattr(tg, "PortfolioManager", FakeManager)
    try:
        with pytest.raises(OrderSubmissionError, match="disconnected"):
            tg.start(str(config_path), without_ibc=True)
        assert captured == {"manage_calls": 1, "cancelled": True, "disconnects": 1}
        config = tg.Config(**tomlkit.parse(config_path.read_text()).unwrap())
        path = tg.account_state_path(
            config.runtime.execution.state_directory, config.runtime.account.number
        )
        with tg.account_lock(path):
            pass
        assert not [task for task in asyncio.all_tasks(loop) if not task.done()]
    finally:
        loop.close()
