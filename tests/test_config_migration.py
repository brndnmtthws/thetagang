from pathlib import Path

import pytest
import tomlkit
from pydantic import ValidationError

from thetagang.config import Config
from thetagang.config_migration.io_safe import atomic_write, choose_backup_path
from thetagang.config_migration.migrate_v1_to_v2 import migrate_v1_to_v2
from thetagang.config_migration.migration_report import build_migration_report
from thetagang.config_migration.schema_detect import SchemaKind, detect_schema
from thetagang.config_migration.startup_migration import (
    MigrationDeclinedError,
    MigrationPreviewRedactionError,
    MigrationRequiredError,
    run_startup_migration,
)


@pytest.fixture
def sample_v1_config() -> str:
    return """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[exchange_hours]
exchange = "XNYS"
action_when_closed = "exit"
delay_after_open = 1800
delay_before_close = 1800
max_wait_until_open = 3600

[watchdog]
host = "127.0.0.1"
port = 7497

[ibc]

[symbols.AAA]
weight = 1.0
"""


def test_detect_schema_v1(sample_v1_config: str) -> None:
    assert detect_schema(sample_v1_config) == SchemaKind.v1


def test_migration_is_deterministic(sample_v1_config: str) -> None:
    first = migrate_v1_to_v2(sample_v1_config)
    second = migrate_v1_to_v2(sample_v1_config)
    assert first.migrated_text == second.migrated_text


def test_migrated_output_validates_as_v2_and_converts_to_legacy(
    sample_v1_config: str,
) -> None:
    result = migrate_v1_to_v2(sample_v1_config)
    config = Config(**tomlkit.parse(result.migrated_text).unwrap())
    assert config.runtime.account.number == "DUX"
    assert "AAA" in config.portfolio.symbols
    assert config.target.dte == 30
    assert config.roll_when.dte == 7
    assert config.runtime.exchange_hours.exchange == "XNYS"
    assert config.runtime.watchdog.port == 7497


def test_startup_migration_noninteractive_requires_explicit_approval(
    tmp_path: Path, sample_v1_config: str
) -> None:
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text(sample_v1_config, encoding="utf8")

    with pytest.raises(MigrationRequiredError) as exc_info:
        run_startup_migration(
            str(config_path),
            migrate_only=False,
            auto_approve=False,
            stdin_isatty=False,
            stdout_isatty=False,
        )
    assert "--config " in str(exc_info.value)
    assert " --migrate-config --yes" in str(exc_info.value)


def test_startup_migration_noninteractive_quotes_config_path_with_spaces(
    tmp_path: Path, sample_v1_config: str
) -> None:
    config_dir = tmp_path / "path with spaces"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "thetagang.toml"
    config_path.write_text(sample_v1_config, encoding="utf8")

    with pytest.raises(MigrationRequiredError) as exc_info:
        run_startup_migration(
            str(config_path),
            migrate_only=False,
            auto_approve=False,
            stdin_isatty=False,
            stdout_isatty=False,
        )
    assert f"--config '{config_path}' --migrate-config --yes" in str(exc_info.value)


def test_startup_migration_decline_makes_no_changes(
    monkeypatch, tmp_path: Path, sample_v1_config: str
) -> None:
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text(sample_v1_config, encoding="utf8")
    original = config_path.read_text(encoding="utf8")

    monkeypatch.setattr("click.confirm", lambda *_args, **_kwargs: False)

    with pytest.raises(MigrationDeclinedError):
        run_startup_migration(
            str(config_path),
            migrate_only=False,
            auto_approve=False,
            stdin_isatty=True,
            stdout_isatty=True,
        )

    assert config_path.read_text(encoding="utf8") == original
    assert not (tmp_path / "thetagang.toml.old").exists()


def test_startup_migration_writes_backup_and_new_file(
    tmp_path: Path, sample_v1_config: str
) -> None:
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text(sample_v1_config, encoding="utf8")

    result = run_startup_migration(
        str(config_path),
        migrate_only=False,
        auto_approve=True,
        stdin_isatty=False,
        stdout_isatty=False,
    )

    assert result.was_migrated is True
    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert detect_schema(config_path.read_text(encoding="utf8")) == SchemaKind.v2


def test_startup_migration_preview_redacts_sensitive_values(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "thetagang.toml"
    raw = """
[account]
number = "DU1234567"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[ibc]
userid = "ib-user"
password = "super-secret"
fixuserid = "fix-user"
fixpassword = "fix-secret"

[database]
url = "postgresql://user:pass@localhost/db"

[symbols.AAA]
weight = 1.0
"""
    config_path.write_text(raw, encoding="utf8")

    prints: list[str] = []

    class StubConsole:
        def print(self, *args: object, **_kwargs: object) -> None:
            for arg in args:
                if isinstance(arg, str):
                    prints.append(arg)

    monkeypatch.setattr(
        "thetagang.config_migration.startup_migration.Console", lambda: StubConsole()
    )

    run_startup_migration(
        str(config_path),
        migrate_only=False,
        auto_approve=True,
        stdin_isatty=True,
        stdout_isatty=True,
    )

    preview = next(
        item
        for item in prints
        if "[meta]" in item and "schema_version = 2" in item and "[runtime." in item
    )
    assert "super-secret" not in preview
    assert "fix-secret" not in preview
    assert "postgresql://user:pass@localhost/db" not in preview
    assert "DU1234567" not in preview
    assert preview.count("[REDACTED]") >= 4

    migrated = config_path.read_text(encoding="utf8")
    assert "super-secret" in migrated
    assert "postgresql://user:pass@localhost/db" in migrated


def test_startup_migration_preview_fails_closed_when_redaction_parse_fails(
    monkeypatch, tmp_path: Path, sample_v1_config: str
) -> None:
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text(sample_v1_config, encoding="utf8")
    original = config_path.read_text(encoding="utf8")

    real_parse = tomlkit.parse

    def failing_parse(value: str):
        if "[meta]" in value and "schema_version = 2" in value:
            raise ValueError("parse failed")
        return real_parse(value)

    monkeypatch.setattr(
        "thetagang.config_migration.startup_migration.tomlkit.parse", failing_parse
    )

    with pytest.raises(MigrationPreviewRedactionError):
        run_startup_migration(
            str(config_path),
            migrate_only=False,
            auto_approve=True,
            stdin_isatty=True,
            stdout_isatty=True,
        )

    assert config_path.read_text(encoding="utf8") == original
    assert not (tmp_path / "thetagang.toml.old").exists()


def test_backup_path_numbering(tmp_path: Path) -> None:
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text("x=1\n", encoding="utf8")
    (tmp_path / "thetagang.toml.old").write_text("old", encoding="utf8")
    (tmp_path / "thetagang.toml.old.1").write_text("old", encoding="utf8")

    backup = choose_backup_path(config_path)
    assert backup.name == "thetagang.toml.old.2"


def test_atomic_write_failure_keeps_original_file(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "thetagang.toml"
    path.write_text("original = true\n", encoding="utf8")

    def explode(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr("thetagang.config_migration.io_safe.os.replace", explode)

    with pytest.raises(OSError):
        atomic_write(path, "new = true\n")

    assert path.read_text(encoding="utf8") == "original = true\n"
    assert not list(tmp_path.glob(".thetagang.toml.*"))


def test_atomic_write_directory_fsync_failure_is_non_fatal(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "thetagang.toml"
    path.write_text("original = true\n", encoding="utf8")
    real_fsync = __import__("os").fsync
    calls = {"n": 0}

    def fsync_maybe_fail(fd):
        calls["n"] += 1
        # first fsync is for temp file; second is directory fsync.
        if calls["n"] >= 2:
            raise OSError("dir fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr("thetagang.config_migration.io_safe.os.fsync", fsync_maybe_fail)

    atomic_write(path, "new = true\n")
    assert path.read_text(encoding="utf8") == "new = true\n"


def test_migration_uses_legacy_normalization_for_parts() -> None:
    raw = """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[ibc]

[symbols.AAA]
parts = 1
[symbols.BBB]
parts = 3
"""
    migrated = migrate_v1_to_v2(raw)
    parsed = tomlkit.parse(migrated.migrated_text).unwrap()
    assert "parts" not in parsed["portfolio"]["symbols"]["AAA"]
    assert parsed["portfolio"]["symbols"]["AAA"]["weight"] == pytest.approx(0.25)
    assert parsed["portfolio"]["symbols"]["BBB"]["weight"] == pytest.approx(0.75)


def test_migration_fails_on_invalid_legacy_config() -> None:
    raw = """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[ibc]

[symbols.AAA]
weight = 0.6
[symbols.BBB]
weight = 0.6
"""
    with pytest.raises(ValidationError):
        migrate_v1_to_v2(raw)


def test_migration_handles_missing_ibc_section() -> None:
    raw = """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[symbols.AAA]
weight = 1.0
"""
    migrated = migrate_v1_to_v2(raw)
    parsed = tomlkit.parse(migrated.migrated_text).unwrap()
    assert parsed["runtime"]["account"]["number"] == "DUX"
    assert parsed["portfolio"]["symbols"]["AAA"]["weight"] == pytest.approx(1.0)


def test_migration_preserves_comments_for_moved_sections() -> None:
    raw = """
# top
[account]
# keep this comment
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[ibc]

[symbols.AAA]
weight = 1.0
"""
    migrated = migrate_v1_to_v2(raw)
    assert "# keep this comment" in migrated.migrated_text


def test_migration_moves_buy_sell_keys_into_overrides_only() -> None:
    raw = """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[ibc]

[symbols.AAA]
weight = 1.0
buy_only_rebalancing = true
sell_only_rebalancing = false
"""
    migrated = migrate_v1_to_v2(raw)
    parsed = tomlkit.parse(migrated.migrated_text).unwrap()

    symbol = parsed["portfolio"]["symbols"]["AAA"]
    assert "buy_only_rebalancing" not in symbol
    assert "sell_only_rebalancing" not in symbol
    assert (
        parsed["strategies"]["wheel"]["equity_rebalance"]["symbol_overrides"]["AAA"][
            "mode"
        ]
        == "buy_only"
    )


def test_migration_moves_wheel_symbol_threshold_overrides_into_strategy_overrides() -> (
    None
):
    raw = """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[ibc]

[symbols.AAA]
weight = 1.0
write_calls_only_min_threshold_percent = 0.05
"""
    migrated = migrate_v1_to_v2(raw)
    parsed = tomlkit.parse(migrated.migrated_text).unwrap()
    symbol = parsed["portfolio"]["symbols"]["AAA"]
    assert "write_calls_only_min_threshold_percent" not in symbol
    assert parsed["strategies"]["wheel"]["symbol_overrides"]["AAA"][
        "write_calls_only_min_threshold_percent"
    ] == pytest.approx(0.05)


def test_migration_copies_global_write_call_threshold_into_wheel_defaults() -> None:
    raw = """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[write_when.calls]
min_threshold_percent = 0.01

[ibc]

[symbols.AAA]
weight = 1.0
"""
    migrated = migrate_v1_to_v2(raw)
    parsed = tomlkit.parse(migrated.migrated_text).unwrap()
    assert parsed["strategies"]["wheel"]["defaults"][
        "write_calls_only_min_threshold_percent"
    ] == pytest.approx(0.01)


def test_migration_preserves_explicit_default_valued_sections() -> None:
    raw = """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[ibc]

[exchange_hours]
exchange = "XNYS"
action_when_closed = "exit"
delay_after_open = 1800
delay_before_close = 1800
max_wait_until_open = 3600

[watchdog]
host = "127.0.0.1"
port = 7497

[symbols.AAA]
weight = 1.0
"""
    migrated = migrate_v1_to_v2(raw)
    parsed = tomlkit.parse(migrated.migrated_text).unwrap()
    runtime = parsed["runtime"]

    assert "exchange_hours" in runtime
    assert "watchdog" in runtime


def test_migration_does_not_materialize_absent_strategy_sections() -> None:
    raw = """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[ibc]

[symbols.AAA]
weight = 1.0
"""
    migrated = migrate_v1_to_v2(raw)
    parsed = tomlkit.parse(migrated.migrated_text).unwrap()
    strategies = parsed["strategies"]
    assert "regime_rebalance" not in strategies
    assert "vix_call_hedge" not in strategies
    assert "cash_management" not in strategies


def test_migration_with_parts_does_not_inject_symbol_defaults() -> None:
    raw = """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[ibc]

[symbols.AAA]
parts = 1
"""
    migrated = migrate_v1_to_v2(raw)
    parsed = tomlkit.parse(migrated.migrated_text).unwrap()
    assert "primary_exchange" not in parsed["portfolio"]["symbols"]["AAA"]


def test_migration_report_contains_mapping_and_warnings_sections(
    sample_v1_config: str,
) -> None:
    result = migrate_v1_to_v2(sample_v1_config)
    report = build_migration_report(result)
    assert "## Key Mappings" in report
    assert "## Warnings" in report
    assert "`symbols` -> `portfolio.symbols`" in report


def test_migration_regime_enabled_non_shares_only_uses_explicit_stage_plan() -> None:
    raw = """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[ibc]

[regime_rebalance]
enabled = true
shares_only = false
symbols = ["AAA"]

[symbols.AAA]
weight = 1.0
"""
    migrated = migrate_v1_to_v2(raw)
    parsed = tomlkit.parse(migrated.migrated_text).unwrap()
    run = parsed["run"]
    assert "stages" in run
    assert "strategies" not in run
    stage_ids = [stage["id"] for stage in run["stages"]]
    assert stage_ids == [
        "options_write_puts",
        "options_write_calls",
        "equity_regime_rebalance",
        "equity_buy_rebalance",
        "equity_sell_rebalance",
        "options_roll_positions",
        "options_close_positions",
    ]
    assert any("explicit run.stages" in warning for warning in migrated.warnings), (
        migrated.warnings
    )


def test_migration_regime_shares_only_excludes_option_stages() -> None:
    raw = """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[ibc]

[regime_rebalance]
enabled = true
shares_only = true
symbols = ["AAA"]

[symbols.AAA]
weight = 1.0
"""
    migrated = migrate_v1_to_v2(raw)
    parsed = tomlkit.parse(migrated.migrated_text).unwrap()
    stage_ids = [stage["id"] for stage in parsed["run"]["stages"]]
    assert "options_write_puts" not in stage_ids
    assert "options_write_calls" not in stage_ids
    assert "options_roll_positions" not in stage_ids
    assert "options_close_positions" not in stage_ids
    assert stage_ids == [
        "equity_regime_rebalance",
        "equity_buy_rebalance",
        "equity_sell_rebalance",
    ]


def test_golden_migration_output_subset_for_stable_shape() -> None:
    raw = """
[account]
number = "DUX"
margin_usage = 0.5

[option_chains]
expirations = 4
strikes = 10

[target]
dte = 30
minimum_open_interest = 5

[roll_when]
dte = 7

[ibc]

[symbols.AAA]
weight = 1.0
buy_only_rebalancing = true
"""
    migrated = migrate_v1_to_v2(raw).migrated_text
    assert "[meta]" in migrated
    assert "schema_version = 2" in migrated
    assert "strategies = [" in migrated
    assert "[runtime.account]" in migrated
    assert "[portfolio.symbols.AAA]" in migrated
    assert "[strategies.wheel.defaults.target]" in migrated
    assert "[strategies.wheel.equity_rebalance.symbol_overrides.AAA]" in migrated
    assert 'mode = "buy_only"' in migrated
