from pathlib import Path

import tomlkit

import thetagang.thetagang as tg
from thetagang.config import SHARES_ONLY_DEPRECATION_MESSAGE


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


def test_start_warns_when_deprecated_shares_only_is_configured(monkeypatch, tmp_path):
    config_doc = tomlkit.parse(
        Path("thetagang.toml").read_text(encoding="utf8")
    ).unwrap()
    config_doc["runtime"]["database"]["enabled"] = False
    config_doc["runtime"]["ib_async"]["logfile"] = ""
    config_doc["strategies"]["regime_rebalance"] = {"shares_only": False}
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text(
        tomlkit.dumps(tomlkit.item(config_doc)),
        encoding="utf8",
    )
    warnings: list[str] = []

    monkeypatch.setattr(tg.Config, "display", lambda *_args: None)
    monkeypatch.setattr(tg, "need_to_exit", lambda *_args: True)
    monkeypatch.setattr(tg.log, "warning", lambda message: warnings.append(message))

    tg.start(str(config_path), dry_run=True)

    assert warnings == [SHARES_ONLY_DEPRECATION_MESSAGE]
