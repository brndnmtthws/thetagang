# External decision providers

ThetaGang can delegate narrowly scoped decisions to an external process while
retaining control of validation, risk limits, and order execution. Providers are
registered once under `runtime.external_decisions.providers`, so future decision
points can reuse the same transport without sharing their decision-specific
schemas.

The versioned [JSON schemas](external-decisions/schemas/) and
[complete request/response examples](../examples/external_decisions/) are the
provider-facing contract. Providers do not need to import or install ThetaGang.

The supported decision types are `regime_target_weights` and
`tail_hedge_harvest`. Target-weight decisions apply bounded per-symbol
multipliers after volatility weighting and before the built-in absolute-trend
modifier. Tail-harvest decisions approve or veto an opportunity that already
passed ThetaGang's allocation gates. External processes cannot submit orders or
bypass drift bands, cooldowns, ratio gates, cash-flow rails, ownership checks,
or execution policies.

## Command protocol

A command provider is executed directly without a shell. ThetaGang writes one
JSON request to stdin, waits for the process to exit, and reads one JSON response
from stdout. Provider diagnostics should be written to stderr. Inference must not
submit trades or mutate ThetaGang's strategy state. Keep training, feature
engineering, model artifacts, and model dependencies in the provider project.
With a fixed model artifact, inference should be deterministic for the complete
supplied request context; the request ID is for correlation, not a model feature.

Timeouts, cancellation, and oversized responses terminate the command and drain
its pipes. On POSIX systems, cleanup also terminates its process group so child
processes cannot keep those pipes open.

Every request uses a generic versioned envelope:

```json
{
  "schema_version": 1,
  "request_id": "3b93bda0-f5f0-4bb7-a40e-34b04733b29a",
  "decision_type": "regime_target_weights",
  "generated_at": "2026-09-03T14:30:00Z",
  "dry_run": true,
  "input": {}
}
```

All envelope datetimes must include a UTC offset. ThetaGang emits
`generated_at` in UTC. Both requests and responses must explicitly include
`schema_version` as the integer `1`; omitted or coerced versions are rejected.

The `input` for `regime_target_weights` contains:

- Strategy settings that affect the eventual rebalance, including the capital
  base, margin usage, drift bands, and cooldown.
- Available broker account metrics, the resolved rebalance base, and excluded
  option value. The broker account number is not sent.
- Configured, current, and post-volatility weights for every managed regime
  symbol, along with shares, prices, values, the volatility configuration and
  calculation, the configured absolute-trend rule, and effective trading and
  minimum-order constraints.
- The host-enforced multiplier constraints and optional target-weight bounds for
  adjustable symbols.
- Aligned completed-session daily close history for the configured feature
  universe. All close arrays correspond exactly to the supplied `sessions`
  array. Data is fetched from IBKR as regular-trading-hours `TRADES` bars.

Explicit primary-exchange overrides use separate persistent history entries,
so missing API bars cannot be filled with another listing's cached prices.

The listed TQQQ sizing features—returns, moving-average distance, trend,
realized volatility, volatility acceleration, drawdown, close-based choppiness,
efficiency, relative trends, correlations, and PCA concentration—can all be
derived from this aligned close matrix. A future schema version can add other
bar fields without changing the provider transport.

The response uses the same generic envelope and a decision-specific `output`.
`as_of_session` is optional in the reusable envelope, but required for
`regime_target_weights`:

```json
{
  "schema_version": 1,
  "request_id": "3b93bda0-f5f0-4bb7-a40e-34b04733b29a",
  "decision_type": "regime_target_weights",
  "as_of_session": "2026-09-02",
  "expires_at": "2026-09-04T14:30:00Z",
  "producer": {
    "name": "tqqq-xgboost-sizing",
    "version": "2026-08-31"
  },
  "output": {
    "adjustments": {
      "TQQQ": {
        "multiplier": 1.07,
        "reason": "moderate-risk-on"
      }
    }
  }
}
```

The response must contain exactly the symbols configured for that decision
point, and every multiplier must be a JSON number rather than a string or
boolean. ThetaGang rejects stale, expired, future-dated, non-finite, unbounded,
or otherwise malformed decisions. It then calculates the target itself:

```text
raw target = post-volatility target * multiplier
effective target = raw target clamped to the enabled target and volatility bounds
final target = effective target * absolute-trend multiplier
```

Each symbol may set `min_target_weight` and/or `max_target_weight`, independently
of its multiplier limits and volatility configuration. These are absolute
fractions of the strategy's configured capital base (`weight_base` with its
resolved `margin_usage`), applied after multiplication and before absolute trend.
Each omitted bound imposes no additional limit. Bounds must be finite values in
`[0, 1]`, and the minimum cannot exceed the maximum. The original volatility
calculation, bounds, and smoothing state remain unchanged.

`clamp_to_volatility_bounds = true` retains the original volatility clamp as
well: the effective interval is the intersection of the two sets of bounds.
Conflicting intervals are rejected. Set it to `false` to let the external policy
use a wider target interval. For example, with IBIT target bounds `[0.10, 0.36]`,
`0.1875 * 0.50` is floored from `0.09375` to `0.10`. Absolute trend can subsequently
reduce the target below this floor. These bounds apply to accepted external
adjustments; `on_error = "baseline"` still uses the post-volatility baseline.

By default an external decision may fill unused allocation up to 100%, but it
cannot increase total exposure beyond the larger of 100% or the pre-decision
total. The request reports this resolved ceiling as
`total_weight_constraint.effective_max_total_weight`.
`max_total_weight` explicitly authorizes a higher ceiling. The
`managed_stocks` capital base is not supported because changing only one symbol
would violate that mode's required 100% total.

The accepted decision is reused for any replanning within the same ThetaGang
run. This prevents a tail-harvest replan from receiving a different model signal
mid-execution.
Its expiry is checked again before reuse; an expired decision follows the
configured failure policy without invoking the provider again.
Any response or target-application failure keeps the hook in its configured
failure behavior for the rest of that run, even if later baseline weights would
make a rejected adjustment fit within the exposure ceiling.

## Tail-harvest decision

ThetaGang requests `tail_hedge_harvest` only after the existing strategy has an
approved same-symbol hard-underweight stock buy, the state-owned tail sleeve is
above its trigger, and at least one active state-owned put is profitable at its
current host-selected limit-price quote. The request `input` contains:

- The harvest trigger and target, resolved regime base, current sleeve value and
  weight, target sleeve value, sale budget, approved stock-rebalance value, and
  the exact host-planned contracts and quantities at the current quotes.
- Every open state-owned hedge cohort with its option contract, entry and
  recovery cost, state-owned quantity and value, full broker quantity, value
  and P&L, current quote, and the host's net-profit candidate calculation.
- Each protected underlying's configured and effective target context, broker
  shares, value, cost basis and P&L, live or planned shares and values, approved
  buy size, tail-program parameters, and volatility, external-weight, and
  absolute-trend modifier details.
- Aligned regular-hours completed-session closes for every protected underlying
  plus optional feature-only reference symbols.

The response must match the request identity, use the latest permitted supplied
session, and return a JSON boolean. Expiry is rechecked after the final quote
refresh; if it has elapsed, the configured failure policy applies:

```json
{
  "schema_version": 1,
  "request_id": "3b93bda0-f5f0-4bb7-a40e-34b04733b29a",
  "decision_type": "tail_hedge_harvest",
  "as_of_session": "2026-09-02",
  "expires_at": "2026-09-04T14:30:00Z",
  "producer": {
    "name": "tail-harvest-policy",
    "version": "2026-08-31"
  },
  "output": {
    "harvest": false,
    "reason": "drawdown-still-accelerating"
  }
}
```

This is an approval gate, not a sizing API. A provider cannot turn `false` host
eligibility into a harvest, name contracts, choose quantities, or set prices.
After a valid approval, ThetaGang re-quotes the options and repeats all band,
ownership, conflict, and profitability checks before persisting recovery intent
or queuing an order.

## Configuration

```toml
[runtime.external_decisions.providers.tqqq_sizing]
transport = "command"
command = ["/opt/tqqq-policy/.venv/bin/python", "-m", "tqqq_policy"]
timeout_seconds = 10
max_response_bytes = 1048576
# working_directory = "/opt/tqqq-policy"

[strategies.regime_rebalance.target_weight_policy]
enabled = true
provider = "tqqq_sizing"
on_error = "baseline" # baseline | abort
max_signal_age_sessions = 0
# max_total_weight = 1.10

[strategies.regime_rebalance.target_weight_policy.symbols.TQQQ]
min_multiplier = 0.50
max_multiplier = 1.20
min_target_weight = 0.15
max_target_weight = 0.55
clamp_to_volatility_bounds = false

[strategies.regime_rebalance.target_weight_policy.symbols.IBIT]
min_multiplier = 0.50
max_multiplier = 1.20
min_target_weight = 0.10
max_target_weight = 0.36
clamp_to_volatility_bounds = false

[strategies.regime_rebalance.target_weight_policy.market_data]
lookback_days = 252
include_strategy_symbols = true

# Reference symbols need not be traded portfolio symbols, but they need an IBKR
# primary exchange so ThetaGang can request unambiguous contracts.
[strategies.regime_rebalance.target_weight_policy.market_data.symbols.QQQ]
primary_exchange = "NASDAQ"

[strategies.regime_rebalance.target_weight_policy.market_data.symbols.IBIT]
primary_exchange = "NASDAQ"

[runtime.external_decisions.providers.tail_harvest]
command = ["/opt/tail-policy/.venv/bin/python", "-m", "tail_policy"]
timeout_seconds = 10

[strategies.tail_hedge.harvest_decision]
enabled = true
provider = "tail_harvest"
on_error = "baseline" # baseline | skip | abort
max_signal_age_sessions = 0

[strategies.tail_hedge.harvest_decision.market_data]
lookback_days = 252
include_strategy_symbols = true

[strategies.tail_hedge.harvest_decision.market_data.symbols.SPY]
primary_exchange = "ARCA"
```

`on_error = "baseline"` retains the unmodified post-volatility target when
history collection, process execution, or response validation fails. The
failure is visible in logs and persisted decision telemetry. When that target
policy controls a tail-hedge underlying, harvesting is not allowed to fund the
symbol unless the sizing signal was fresh and valid. `on_error = "abort"`
aborts regime-rebalance planning instead.

For `tail_hedge_harvest`, `baseline` preserves the existing eligible harvest,
`skip` declines it, and `abort` stops planning. A valid provider veto is not an
error and is honored regardless of the failure setting.

The provider is trusted local code and runs with the same operating-system
permissions as ThetaGang. Process separation isolates Python dependencies; it is
not a security sandbox.

## Build and check a provider offline

The [reference provider](../examples/external_decisions/provider.py) uses only
Python's standard library. It returns a neutral target multiplier and vetoes
harvesting, demonstrating both response shapes. Replace its `decide()` function
in your own project with feature preparation and inference using a fixed model
artifact. Use `producer.version` to identify the code/model artifact that
produced a decision.

From the repository root, run the real command transport and validators without
starting the trading runtime or connecting to IBKR:

```sh
uv run python -m thetagang.decision_check check \
  --request examples/external_decisions/regime_target_weights.request.json \
  -- python3 -I -S examples/external_decisions/provider.py

uv run python -m thetagang.decision_check check \
  --request examples/external_decisions/tail_hedge_harvest.request.json \
  -- python3 -I -S examples/external_decisions/provider.py
```

`-I -S` demonstrates that the reference provider needs no installed packages.
For your real provider, replace the command after `--` with its environment's
Python and entry point, for example `/opt/my-policy/.venv/bin/python -m my_policy`.
Options such as `--timeout-seconds`, `--max-response-bytes`, and
`--working-directory` configure the same transport used during live planning.

You can also replay a captured response without executing a provider:

```sh
uv run python -m thetagang.decision_check check \
  --request examples/external_decisions/regime_target_weights.request.json \
  --response examples/external_decisions/regime_target_weights.response.json \
  --at 2026-09-04T14:30:00Z
```

The checker defaults to the request's `generated_at` as the replay validation
time and prints the time it used. Use `--at` to reproduce validation at a later
instant, including expiry during inference. Historical replay does not establish
that a signal is fresh today. `--max-signal-age-sessions` defaults to `0` and
`--weight-epsilon` to `1e-8`; set these to the deployment's policy age limit and
regime `eps` when they differ. Multiplier limits, optional target bounds, and
volatility bounds come from the saved request. The checker applies the same
clamps and aggregate exposure ceiling as live planning.

A successful check prints JSON and exits `0`. Invalid requests, failed commands,
and rejected responses exit nonzero; the checker does not hide errors behind the
deployment's baseline fallback. Target checks include the production multiplier,
clamping, and total-exposure checks. `post_policy_weights` are the targets before
absolute-trend controls and subsequent trading gates. A valid harvest response
only passes the external approval contract; it does not replay live ownership,
quotes, allocation bands, or order execution.

## Field semantics and compatibility

Both requests contain all named fields shown in the examples. Nullable fields
are explicitly `null` when that context is unavailable or inapplicable. An
unavailable account metric is omitted from `account.metrics`. Optional response
fields (`expires_at` and `reason`) may be omitted or null.

| Field group | Meaning |
| --- | --- |
| Weights, allocation bands, budgets, drawdowns and percentage thresholds | Fractions: `0.05` means 5%. Regime drift bands compare relative deviation from the target. `margin_usage` is the host capital-base multiplier. |
| Market closes and stock `market_price` | Per-share prices. The current history adapter requests USD stock contracts and regular-hours `TRADES` closes. |
| Stock values, capital bases, costs, proceeds and P&L | Monetary amounts in the host/broker accounting units. Account metrics retain broker units; `Cushion` is a fraction. The decision adapter performs no currency conversion. |
| Option `limit_price`, `quoted_limit_price`, `entry_limit_price` | Quote price before the contract multiplier. Per-contract proceeds/cost fields already include it. Multiply by contract quantity for totals. |
| `current_shares`, broker `shares`, option `quantity` | Stock share counts and option contract counts respectively. `state_owned_quantity` may be less than the full broker position. |
| `sessions` and `closes` | Strictly aligned completed-session history with unique sessions ordered oldest to newest and finite positive numeric closes. A history lookback of N returns normally supplies N+1 closes. No intraday bars are included. |
| Lookbacks, cooldowns and signal age | Trading-session counts. DTE settings use calendar days. Option `expiration` is an IBKR `YYYYMMDD` string. |
| Volatility settings/calculations | Annualized fractional volatility, using 252 trading days. Smoothing factors and efficiency values are dimensionless fractions; choppiness and ratio drift statistics are dimensionless. |
| Datetimes and snapshot context | Offset-aware instants; requests emit `generated_at` in UTC. It is the assembly time, not a guarantee that every broker observation arrived simultaneously. |

`volatility_weight.config = null` means no volatility configuration;
`calculation = null` means no successful calculation is supplied. Protected
underlyings outside the active regime may have null target fields. Underlying
`broker_position` aggregates retain the host's existing zero-filled sums: a zero
does not distinguish an empty position set from unavailable marks. Individual
hedge observations retain nulls for unavailable/non-finite broker values.

Calculation diagnostics (`volatility_weight.calculation` and
`target_modifiers`) have variable keys describing host calculations. Providers
should tolerate new diagnostic keys and use the explicit target and constraint
fields for allocation decisions.

`schema_version = 1` covers both the envelope and the decision-specific shape.
Changes to defined fields, types, units, or meanings require a new contract
version; entries in the explicitly open symbol, metric, and diagnostic maps may
vary within v1. `producer.version` identifies the provider artifact independently
of the protocol version. The checker and runtime request builders validate
against the same models. Regenerate the published schemas with:

```sh
uv run python -m thetagang.decision_check schemas \
  --output-dir docs/external-decisions/schemas
```

Tests check that the published schemas match the models, the complete examples
validate, and the reference provider runs without third-party dependencies.
