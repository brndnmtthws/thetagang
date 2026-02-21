---
applyTo: "thetagang/config.py,thetagang/config_v2.py,thetagang/config_migration/**/*.py,thetagang/main.py"
---
# Config and Migration Review Focus

When reviewing these files:

- Preserve schema invariants: valid stage IDs, dependency ordering, no dependency cycles, and `collect_state` first/enabled.
- Flag any change that weakens validation bounds/defaults for risk-sensitive settings (thresholds, margin usage, DTE/sigma limits).
- Ensure migration remains deterministic and safe: backup handling, atomic writes, and no silent destructive overwrite.
- Verify v2 config compatibility with legacy runtime conversion remains intact.
- Require tests whenever new config keys are added, moved, renamed, or defaulted.
- If CLI behavior changes around migration prompts/flags, require tests for non-interactive and error-reporting paths.
