# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ThetaGang is an automated options trading bot for Interactive Brokers (IBKR) that implements "The Wheel" strategy - selling cash-secured puts and covered calls to generate income from option premiums.

## Development Commands

### Running the application
```bash
# Always use uv to run the application
uv run thetagang --config thetagang.toml

# Dry run mode (no actual trades)
uv run thetagang --config thetagang.toml --dry-run

# Without IBC (when TWS/Gateway is already running)
uv run thetagang --config thetagang.toml --without-ibc
```

### Testing
```bash
# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_portfolio_manager.py

# Run with coverage
uv run pytest --cov=thetagang

# Watch mode for development
uv run pytest-watch
```

### Code Quality
```bash
# Run linting
uv run ruff check .

# Run formatting
uv run ruff format .

# Type checking
uv run pyright

# Run pre-commit hooks
uv run pre-commit run --all-files
```

## Architecture Overview

### Core Components

1. **Entry Point** (`thetagang/thetagang.py`):
   - Initializes IB connection and IBC controller
   - Sets up event loop with ib_async
   - Creates PortfolioManager instance
   - Handles graceful shutdown

2. **Portfolio Manager** (`thetagang/portfolio_manager.py`):
   - Core trading logic implementation
   - Main `manage()` loop that:
     - Analyzes account positions
     - Determines when to write new options
     - Evaluates existing positions for rolling/closing
     - Handles order submission and management

3. **Configuration** (`thetagang/config.py`):
   - Pydantic models for type-safe configuration
   - Key configs: `Config`, `SymbolConfig`, `RollWhenConfig`, `WriteWhenConfig`
   - Loaded from `thetagang.toml` file

4. **IBKR Integration** (`thetagang/ibkr.py`):
   - Wrapper around ib_async library
   - Handles all API calls to Interactive Brokers
   - Manages market data subscriptions
   - Order placement and monitoring

### Key Trading Logic

- **Put Writing**: Sells puts when underlying is red (configurable) and buying power allows
- **Call Writing**: Sells calls against stock positions when underlying is green (configurable)
- **Position Rolling**: Evaluates positions based on P&L, DTE, and ITM status
- **Greeks-based**: Uses delta for strike selection
- **Risk Management**: Enforces margin limits, position caps, and strike boundaries

### Important Patterns

1. **Async/Await**: All IBKR interactions are async - use `await` for API calls
2. **Event-driven**: Uses ib_async events for real-time updates
3. **Configuration-driven**: Most behavior controlled via `thetagang.toml`
4. **Dry run support**: Always test changes with `--dry-run` first

### Common Development Tasks

When modifying trading logic:
1. Start in `portfolio_manager.py` - this contains the main strategy implementation
2. Test configuration changes in `thetagang.toml` with dry run mode
3. For new features, add configuration in `config.py` with Pydantic models
4. IBKR API calls go through `ibkr.py` wrapper methods

When debugging issues:
1. Check logs - the bot uses structured logging with Rich
2. Verify market hours in `exchange_hours.py`
3. For order issues, check `orders.py` and `trades.py`
4. For position analysis, see utilities in `util.py`

### Event Loop Considerations

The project uses ib_async which patches asyncio with nest_asyncio. When creating futures or async tasks:
- Use `util.getLoop().create_future()` instead of `asyncio.Future()`
- This ensures compatibility with ib_async's event loop management

### Testing Approach

- Unit tests use pytest with pytest-asyncio for async code
- Mock IBKR interactions using pytest-mock
- Test configuration validation with various TOML inputs
- Integration tests should use paper trading account

### Configuration File

The main configuration file is `thetagang.toml`. Key sections:
- `account`: Account settings, margin usage
- `orders`: Order routing, algorithms
- `symbols`: List of symbols with individual configurations
- `roll_when`: Conditions for rolling positions
- `write_when`: Conditions for writing new contracts
- `vix_call_hedge`: Optional VIX hedging
- `cash_management`: Optional cash position management
