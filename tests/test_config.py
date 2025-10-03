from thetagang.config import Config


def test_config_from_dict_minimal() -> None:
    raw = {"account": {"number": "DU123"}}
    config = Config.from_dict(raw)

    assert config.account.number == "DU123"
    assert config.account.market_data_type == 1
    assert config.connection.host == "127.0.0.1"
    assert config.connection.port == 7497
    assert config.connection.client_id == 1


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
