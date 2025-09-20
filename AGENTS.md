# Repository Guidelines

## Project Structure & Module Organization
- Core trading code lives in `thetagang/`; the CLI entry point is `thetagang/entry.py`, with the main orchestration in `portfolio_manager.py` and configuration models
in `config.py`.
- Broker integrations and execution helpers sit in `thetagang/ibkr.py`, `orders.py`, and related utilities under the same package.
- Supporting scripts and assets reside in `tws/`, `lib/`, and the packaging files `pyproject.toml` and `uv.lock`; sample configs and data are under `data/` and
`thetagang.toml`.
- Tests mirror the module layout inside `tests/`; fixtures and async helpers are centralized in `tests/conftest.py`.

## Build, Test, and Development Commands
- `uv run thetagang --config thetagang.toml --dry-run` — execute the bot without submitting live trades.
- `uv run pytest` — run the full test suite; append a path (e.g., `tests/test_portfolio_manager.py`) to scope runs.
- `uv run pytest --cov=thetagang` — gather coverage for trading logic changes.
- `uv run ruff check .` / `uv run ruff format .` — lint and auto-format the codebase.
- `uv run pyright` — perform static type checking.
- `uv run pre-commit run --all-files` — replicate the CI hook set before pushing.

## Coding Style & Naming Conventions
- Python ≥3.10 with 4-space indentation and Ruff-enforced 88 character lines; keep imports sorted via Ruff.
- Use snake_case for functions and variables, CapWords for classes, and descriptive config keys; follow existing naming inside `portfolio_manager.py`.
- Annotate new or modified functions with precise type hints and keep module-level constants uppercase.
- Add configuration-driven behavior through Pydantic models in `config.py`, ensuring defaults and validation match `thetagang.toml`.

## Testing Guidelines
- Tests rely on `pytest` and `pytest-asyncio`; name files `test_<module>.py` and prefer async tests for IBKR flows.
- Stub external calls with fixtures from `tests/` to prevent network usage; extend them when adding API paths.
- Cover edge cases for buy/sell-only rebalancing and order routing when adjusting portfolio logic.
- Run `uv run pytest --cov=thetagang` before submitting changes and ensure new features include targeted assertions.

## Commit & Pull Request Guidelines
- Follow the conventional commit style seen in history (`fix:`, `feat:`, `chore:`) with optional scopes (`fix(portfolio): enforce buy-only thresholds`).
- Keep subject lines in present tense ≤72 characters and use bodies to explain intent, risk, and testing evidence.
- Rebase or squash noisy work-in-progress commits prior to open PRs.
- PR descriptions should note behavioral impacts, list validation commands, link issues, and include relevant logs or screenshots for trading output adjustments.
