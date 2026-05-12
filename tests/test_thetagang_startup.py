from pathlib import Path

import thetagang.thetagang as tg


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
