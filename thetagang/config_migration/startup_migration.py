from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import tomlkit
from rich.console import Console

from .io_safe import atomic_write, write_backup
from .migrate_v1_to_v2 import MigrationResult, migrate_v1_to_v2
from .migration_report import build_migration_report
from .schema_detect import SchemaKind, detect_schema


@dataclass
class MigrationFlowResult:
    schema: SchemaKind
    config_text: str
    was_migrated: bool = False
    migration_result: MigrationResult | None = None
    backup_path: Path | None = None


class MigrationDeclinedError(RuntimeError):
    pass


class MigrationRequiredError(RuntimeError):
    pass


class InvalidMigrationOptionError(RuntimeError):
    pass


class UnknownSchemaError(RuntimeError):
    pass


class MigrationPreviewRedactionError(RuntimeError):
    pass


SENSITIVE_PATHS: set[tuple[str, ...]] = {
    ("runtime", "account", "number"),
    ("runtime", "ibc", "password"),
    ("runtime", "ibc", "userid"),
    ("runtime", "ibc", "fixpassword"),
    ("runtime", "ibc", "fixuserid"),
    ("runtime", "database", "url"),
}


def run_startup_migration(
    config_path: str,
    *,
    migrate_only: bool,
    auto_approve: bool,
    stdin_isatty: bool | None = None,
    stdout_isatty: bool | None = None,
) -> MigrationFlowResult:
    path = Path(config_path)
    raw_text = path.read_text(encoding="utf8")
    schema = detect_schema(raw_text)

    if schema == SchemaKind.v2:
        if auto_approve and not migrate_only:
            raise InvalidMigrationOptionError(
                "--yes is only valid when running --migrate-config or when a legacy v1 "
                "config requires migration."
            )
        return MigrationFlowResult(schema=schema, config_text=raw_text)

    if schema != SchemaKind.v1:
        raise UnknownSchemaError("Unable to detect config schema. Expected v1 or v2.")

    interactive = _is_interactive(
        stdin_isatty=stdin_isatty, stdout_isatty=stdout_isatty
    )
    if not interactive and not auto_approve:
        cmd = f"thetagang --config {shlex.quote(config_path)} --migrate-config --yes"
        raise MigrationRequiredError(
            "Legacy config migration requires confirmation, but no TTY is available. "
            f"Run: {cmd}"
        )

    result = migrate_v1_to_v2(raw_text)
    report = build_migration_report(result)
    console = Console()
    if interactive:
        try:
            preview_text = redact_sensitive_preview_text(result.migrated_text)
        except MigrationPreviewRedactionError as exc:
            raise MigrationPreviewRedactionError(
                "Unable to safely redact migration preview; refusing to print "
                "potentially sensitive configuration."
            ) from exc
        console.print("[bold yellow]Detected legacy config schema (v1).[/bold yellow]")
        console.print("[bold]Generated v2 configuration preview:[/bold]")
        console.print(preview_text, markup=False)
        console.print("[bold]Migration report:[/bold]")
        console.print(report, markup=False)
    else:
        console.print(
            "[bold yellow]Detected legacy config schema (v1). Applying auto-approved migration...[/bold yellow]"
        )

    approved = auto_approve
    if not approved:
        approved = click.confirm(
            "Apply this migration and write the new config file?",
            default=False,
            show_default=True,
        )

    if not approved:
        raise MigrationDeclinedError("Migration declined; no files were modified.")

    backup = write_backup(path)
    mode = path.stat().st_mode
    atomic_write(path, result.migrated_text, mode=mode)

    if migrate_only:
        return MigrationFlowResult(
            schema=SchemaKind.v2,
            config_text=result.migrated_text,
            was_migrated=True,
            migration_result=result,
            backup_path=backup,
        )

    return MigrationFlowResult(
        schema=SchemaKind.v2,
        config_text=result.migrated_text,
        was_migrated=True,
        migration_result=result,
        backup_path=backup,
    )


def _is_interactive(
    *,
    stdin_isatty: bool | None = None,
    stdout_isatty: bool | None = None,
) -> bool:
    in_tty = sys.stdin.isatty() if stdin_isatty is None else stdin_isatty
    out_tty = sys.stdout.isatty() if stdout_isatty is None else stdout_isatty
    return bool(in_tty and out_tty)


def redact_sensitive_preview_text(config_text: str) -> str:
    try:
        doc = tomlkit.parse(config_text)
    except Exception as exc:
        raise MigrationPreviewRedactionError(
            "Failed to parse migrated config for preview redaction."
        ) from exc
    _redact_sensitive_items(doc, path=())
    return tomlkit.dumps(doc)


def _is_sensitive_path(path: tuple[str, ...]) -> bool:
    return path in SENSITIVE_PATHS


def _redact_sensitive_items(item: Any, *, path: tuple[str, ...]) -> None:
    if hasattr(item, "items"):
        for key, value in item.items():
            key_str = str(key)
            next_path = path + (key_str,)
            if _is_sensitive_path(next_path):
                item[key] = "[REDACTED]"
            else:
                _redact_sensitive_items(value, path=next_path)
    elif isinstance(item, list):
        for idx, entry in enumerate(item):
            _redact_sensitive_items(entry, path=path + (str(idx),))
