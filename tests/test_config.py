from thetagang.config import Config, _default_logfile_path


def test_config_from_dict_minimal() -> None:
    raw = {"account": {"number": "DU123"}}
    config = Config.from_dict(raw)

    assert config.account.number == "DU123"
    assert config.account.market_data_type == 1
    assert config.connection.host == "127.0.0.1"
    assert config.connection.port == 7497
    assert config.connection.client_id == 1
    assert config.ib_async.logfile.name == "ib_async.log"


def test_config_records_ignored_sections() -> None:
    raw = {
        "account": {"number": "DU123", "market_data_type": 3},
        "connection": {"host": "ib", "port": 4001, "client_id": 42},
        "symbols": {},
        "orders": {},
    }
    config = Config.from_dict(raw)

    assert config.account.market_data_type == 3
    assert config.connection.host == "ib"
    assert config.ignored_sections == ["orders", "symbols"]


def test_ib_async_resolve_logfile_creates_parent(tmp_path) -> None:
    target = tmp_path / "logs" / "ib.log"
    raw = {"account": {"number": "DU123"}, "ib_async": {"logfile": str(target)}}
    config = Config.from_dict(raw)

    resolved = config.ib_async.resolve_logfile()

    assert resolved == target
    assert resolved.parent.is_dir()


def test_ib_async_resolve_logfile_falls_back(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("cannot be a directory")

    requested = blocked_parent / "ib.log"
    raw = {"account": {"number": "DU123"}, "ib_async": {"logfile": str(requested)}}
    config = Config.from_dict(raw)

    resolved = config.ib_async.resolve_logfile()
    expected_default = _default_logfile_path()

    assert resolved == expected_default
    assert resolved.parent.is_dir()
