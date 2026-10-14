# Repository Guidelines

ThetaGang is a low-frequency, systematic portfolio execution and risk-management tool for Interactive Brokers (IBKR). It executes stage-driven strategies (`wheel`, `regime_rebalance`, `tail_hedge`, `vix_call_hedge`, `cash_management`) with dry-run support and exchange-hours gating.

## Project Structure
- `thetagang/entry.py` & `thetagang/main.py`: Click CLI entry point for the `thetagang` console script.
- `thetagang/thetagang.py`: Startup wiring, config loading, IBKR/IBC setup, event loop, and dry-run control flow.
- `thetagang/portfolio_manager.py`: Stage orchestration and runtime execution across strategies.
- `thetagang/strategies/`: Strategy runners and engines (`equity`, `options`, `regime_engine`, `tail_hedge_engine`, `post`).
- `thetagang/config.py`, `config_models.py`, `legacy_config.py`, `config_migration/`: Pydantic v2 schema validation, stage resolution, and v1-to-v2 migration.
- `thetagang/accounting.py`: Capital base calculations (`net_liquidation`, `wheel_buying_power`, `regime_rebalance`) and position taxonomy.
- `thetagang/ibkr.py`, `order_execution.py`, `orders.py`, `trades.py`: Broker integration, order supervision, repricing, and execution tracking.
- `thetagang/external_decisions.py`: Subprocess protocol for bounded external decision providers.
- `thetagang/db.py`: SQLite persistence for orders, executions, historical bars, snapshots, and decision gates.
- `thetagang.toml`: Sample configuration.
- `tests/`: Pytest suite mirroring `thetagang/`, with shared fixtures in `tests/conftest.py`.

## Build, Test, and Development Commands
- `uv run thetagang --config thetagang.toml --dry-run` — run without submitting live orders.
- `uv run pytest` — run the test suite (append a path like `tests/test_portfolio_manager.py` to scope).
- `uv run pytest --cov=thetagang` — run tests with coverage.
- `uv run ruff check .` / `uv run ruff format .` — lint and format.
- `uv run ty check` — static type checking.
- `uv run pre-commit run --all-files` — run all pre-commit hooks.

## Coding Style & Conventions
- Python ≥3.11, 4-space indentation, 88-character line limit (enforced by Ruff).
- `snake_case` for functions/variables, `CapWords` for classes, `UPPERCASE` for module constants.
- Type-annotate all new or modified functions.
- Define configuration changes via Pydantic models in `config_models.py` and `config.py`, keeping defaults and validation aligned with `thetagang.toml`.

## Testing Guidelines
- Use `pytest` and `pytest-asyncio`; name files `test_<module>.py`.
- Stub all broker and network calls using fixtures in `tests/`.
- Cover safety invariants: `dry_run` gating, exchange-hours checks, stage ordering, rebalancing bands, and option sizing/rolling bounds.

## Commit & Pull Request Guidelines
- Use Conventional Commits (`fix:`, `feat:`, `chore:`) with optional scopes (`fix(portfolio): enforce buy-only thresholds`), present tense, ≤72 characters.
- Squash or rebase noisy WIP commits before opening a PR.
- Document behavioral impact, risk-control implications, and validation commands run in the PR description.
