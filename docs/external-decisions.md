# External decision providers

ThetaGang can delegate narrowly scoped decisions to an external process while
retaining control of validation, risk limits, and order execution. Providers are
registered once under `runtime.external_decisions.providers`, so future decision
points can reuse the same transport without sharing their decision-specific
schemas.

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
from stdout. Provider diagnostics should be written to stderr. Each invocation
must be side-effect free and deterministic for the supplied completed-session
data.

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
`generated_at` in UTC.

The `input` for `regime_target_weights` contains:

- Strategy settings that affect the eventual rebalance, including the capital
  base, margin usage, drift bands, and cooldown.
- Available broker account metrics, the resolved rebalance base, and excluded
  option value. The broker account number is not sent.
- Configured, current, and post-volatility weights for every managed regime
  symbol, along with shares, prices, values, the volatility configuration and
  calculation, the configured absolute-trend rule, and effective trading and
  minimum-order constraints.
- The host-enforced multiplier constraints for adjustable symbols.
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
effective target = raw target clamped to configured volatility bounds
```

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
min_multiplier = 0.80
max_multiplier = 1.10
clamp_to_volatility_bounds = true

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
