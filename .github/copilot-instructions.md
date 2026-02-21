# ThetaGang Copilot Review Instructions

You are reviewing a Python trading-automation system that can place real IBKR orders.
Prioritize behavioral correctness, risk controls, and regression detection over style feedback.

## Review Priorities (highest first)

1. Safety and trading behavior regressions
- Flag any change that could submit live orders unexpectedly, bypass `dry_run`, or weaken exchange-hours gating.
- Flag changes that alter buy/sell quantity calculations, margin usage behavior, strike limits, DTE filters, or rolling/closing criteria without clear tests.
- Flag changes that remove or weaken guardrails around `trading_is_allowed`, buy-only/sell-only thresholds, or regime-rebalance hard/soft band controls.

2. Stage orchestration invariants
- Verify stage IDs and dependencies remain valid and deterministic (`collect_state` first, no cycles, no enabled stage depending on disabled stage).
- Flag changes where stage ordering can execute actions before required state collection or before prerequisite stages.
- Flag behavior changes that desynchronize `config_v2` stage settings from runtime behavior in `PortfolioManager` and strategy runners.

3. IBKR/API resilience
- Flag missing timeout handling, missing retries/fallbacks, or swallowed exceptions in market/account/position fetch paths.
- Flag required market-data-field usage changes that can reduce data integrity (e.g., treating required fields as optional without justification/tests).
- Flag changes that can place orders with unqualified contracts or missing `conId` validation.

4. Config and migration compatibility
- Preserve v2 schema validation guarantees and v2->legacy normalization behavior.
- Flag any change to config migration that could mutate files unsafely (backup, atomic write, recoverability).
- Ensure new config fields are validated with bounds/defaults and are reflected in both runtime behavior and tests.

5. Data persistence and observability
- For DB-related changes, verify behavior in dry run vs live mode is intentional.
- Flag changes that reduce ability to audit decisions/executions/history.

## What Good Review Feedback Looks Like

- Prefer specific, file-anchored findings with concrete impact and a reproduction scenario.
- Prioritize correctness/risk findings above lint/style comments.
- If behavior changes intentionally, ask for or suggest targeted tests.
- Avoid conflicting advice with repository conventions (`uv`, `ruff`, `ty`, pytest).

## Required Validation Expectations for Risky Changes

When changes touch trading logic, recommend validating with:
- `uv run pytest` (or targeted tests under `tests/`)
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run ty check`

For changes affecting run stages, migration, or rebalancing math, call out missing targeted tests as review findings.
