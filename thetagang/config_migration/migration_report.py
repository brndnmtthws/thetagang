from __future__ import annotations

from thetagang.config_migration.migrate_v1_to_v2 import MigrationResult


def build_migration_report(result: MigrationResult) -> str:
    lines: list[str] = []
    lines.append("# ThetaGang Config Migration Report")
    lines.append("")
    lines.append(f"- Source schema: `{result.source_schema}`")
    lines.append(f"- Target schema: `{result.target_schema}`")
    lines.append("")

    lines.append("## Key Mappings")
    if not result.mappings:
        lines.append("- No key mappings recorded.")
    else:
        for mapping in result.mappings:
            note = f" ({mapping.note})" if mapping.note else ""
            lines.append(f"- `{mapping.old_path}` -> `{mapping.new_path}`{note}")
    lines.append("")

    lines.append("## Warnings")
    if not result.warnings:
        lines.append("- None")
    else:
        for warning in result.warnings:
            lines.append(f"- {warning}")
    lines.append("")

    return "\n".join(lines)
