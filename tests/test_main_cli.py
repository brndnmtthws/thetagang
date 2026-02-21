from click.testing import CliRunner

from thetagang.config_migration.startup_migration import MigrationRequiredError
from thetagang.main import cli


def test_cli_passes_migration_flags(monkeypatch, tmp_path):
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text("x=1\n", encoding="utf8")

    captured = {}

    def fake_start(config, without_ibc, dry_run, **kwargs):
        captured["config"] = config
        captured["without_ibc"] = without_ibc
        captured["dry_run"] = dry_run
        captured.update(kwargs)

    monkeypatch.setattr("thetagang.thetagang.start", fake_start)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--config",
            str(config_path),
            "--migrate-config",
            "--yes",
            "--dry-run",
            "--without-ibc",
        ],
    )

    assert result.exit_code == 0
    assert captured["config"] == str(config_path)
    assert captured["without_ibc"] is True
    assert captured["dry_run"] is True
    assert captured["migrate_config"] is True
    assert captured["auto_approve_migration"] is True


def test_cli_handles_migration_required_without_traceback(monkeypatch, tmp_path):
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text("x=1\n", encoding="utf8")

    def fake_start(*_args, **_kwargs):
        raise MigrationRequiredError("Legacy config migration requires confirmation")

    monkeypatch.setattr("thetagang.thetagang.start", fake_start)

    result = CliRunner().invoke(cli, ["--config", str(config_path)])

    assert result.exit_code != 0
    assert "Error: Legacy config migration requires confirmation" in result.output
    assert "Traceback" not in result.output


def test_cli_rejects_yes_without_migration_context(tmp_path):
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text(
        """
[meta]
schema_version = 2

[run]
stages = [{ id = "options_write_puts", kind = "options.write_puts", enabled = true }]

[runtime.account]
number = "DUX"
margin_usage = 0.5

[symbols.AAA]
weight = 1.0
""".strip()
        + "\n",
        encoding="utf8",
    )

    result = CliRunner().invoke(cli, ["--config", str(config_path), "--yes"])

    assert result.exit_code != 0
    assert "--yes is only valid when running --migrate-config" in result.output
    assert "Traceback" not in result.output


def test_cli_handles_unknown_schema_without_traceback(tmp_path):
    config_path = tmp_path / "bad.toml"
    config_path.write_text("not even toml [\n", encoding="utf8")

    result = CliRunner().invoke(cli, ["--config", str(config_path)])

    assert result.exit_code != 0
    assert "Unable to detect config schema. Expected v1 or v2." in result.output
    assert "Traceback" not in result.output
