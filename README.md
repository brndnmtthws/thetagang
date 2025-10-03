# Θ ThetaGang Θ

ThetaGang is now a lightweight command line client for Interactive Brokers (IBKR)
that renders an account summary and your current open positions.  It connects to
an existing Trader Workstation or IB Gateway session and uses the
[`ib-async`](https://github.com/brndnmtthws/ib-async) library to gather account
information in real time.

> **Important:** This project no longer automates any trading strategies.  The
> CLI is read-only and will never submit orders on your behalf.

## Features

- Display the net liquidation value, margin usage, buying power, and cash
  balances for your configured account.
- Show all open positions with quantity, mark price, average price, market
  value, and unrealised P&L metrics.
- Colour-coded output using [Rich](https://github.com/Textualize/rich) for quick
  scanning of gains and losses.

## Installation

You can install the CLI directly from the repository using
[uv](https://github.com/astral-sh/uv):

```bash
uv tool install .
```

Alternatively, clone the repository and run it in-place with `uv run`:

```bash
git clone https://github.com/brndnmtthws/thetagang.git
cd thetagang
uv run thetagang --config thetagang.toml --without-ibc
```

## Configuration

The CLI reads configuration values from a TOML file.  A minimal example is
available at [`thetagang.toml`](./thetagang.toml):

```toml
[account]
number = "DU1234567"
market_data_type = 1

[connection]
host = "127.0.0.1"
port = 7497
client_id = 1
```

- `account.number` — the IBKR account identifier to query.
- `account.market_data_type` — the TWS market data type (1 for live data,
  see the [official docs](https://interactivebrokers.github.io/tws-api/market_data_type.html)).
- `connection.host` / `connection.port` — address of the running Trader
  Workstation or IB Gateway instance.
- `connection.client_id` — client identifier used when connecting to the
  gateway.  Each running client must use a unique value.

Any other sections present in your TOML file are ignored and reported when the
configuration is displayed.

## Usage

1. Start Trader Workstation or IB Gateway and ensure API access is enabled.
2. Run the CLI, pointing it at your configuration file:

   ```bash
   uv run thetagang --config path/to/thetagang.toml --without-ibc
   ```

3. Review the rendered account summary and open positions in your terminal.

The `--without-ibc` flag suppresses the legacy message about automatic IB
Gateway management.  The `--dry-run` flag is retained for backwards
compatibility; the CLI never submits orders so it only adjusts log messaging.

## Example Output

```
╭────────────────────────────── Configuration (thetagang.toml) ──────────────────────────────╮
│ Account number  DU1234567                                                                  │
│ Market data type 1                                                                         │
│                                                                                           │
│ Host            127.0.0.1                                                                  │
│ Port            7497                                                                       │
│ Client ID       1                                                                          │
╰───────────────────────────────────────────────────────────────────────────────────────────╯
╭───────────────────────────────────────────── Account summary ─────────────────────────────╮
│ Net liquidation   $100,000                                                                │
│ Excess liquidity  $50,000                                                                 │
│ Buying power      $200,000                                                                │
│ Total cash        $25,000                                                                 │
│ Cushion           25.00%                                                                  │
╰───────────────────────────────────────────────────────────────────────────────────────────╯
╭───────────────────────────────────────────── Open positions ──────────────────────────────╮
│ Symbol  Description              Position  Mark     Avg price  Market value  Unrealized P&L  P&L % │
│ SPY     SPY   240119C00400000   -1        $1.50    $2.00      -$150.00      $50.00          25.00% │
│ AAPL    AAPL                     5         $190.00  $180.00    $950.00       $50.00          5.56%  │
╰───────────────────────────────────────────────────────────────────────────────────────────╯
```

## Development

Run the test suite with:

```bash
uv run pytest
```

Formatting and linting are handled by [Ruff](https://github.com/astral-sh/ruff):

```bash
uv run ruff format .
uv run ruff check .
```

## License

ThetaGang is distributed under the terms of the [AGPL-3.0-only](./LICENSE).
