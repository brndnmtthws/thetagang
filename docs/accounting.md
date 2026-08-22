# Portfolio accounting

ThetaGang keeps portfolio arithmetic in `thetagang.accounting`. Strategy code
should request an accounting view instead of reading broker tags or rebuilding
capital formulas directly. This keeps broker values, configuration multipliers,
position ownership, and pending cash in one auditable model.

## Account metrics

`AccountMetric` names the broker values used by the application. A
`BrokerAccountSnapshot` selects one aggregate `BASE` value per metric, rejects
ambiguous or non-finite values, and exposes typed accessors for net liquidation
and total cash. Broker-reported `BuyingPower` remains a displayed metric; it is
not interchangeable with ThetaGang's configured strategy capital.

## Capital bases

`CapitalBaseKind` distinguishes three deliberately different views:

| Base | Calculation | Consumers |
| --- | --- | --- |
| `net_liquidation` | broker NLV | tail-entry and VIX hedge budgets, NLV-relative thresholds |
| `wheel_buying_power` | `floor(NLV * wheel margin_usage)` | wheel targets and ordinary equity rebalancing |
| `regime_rebalance` | configured `weight_base` with regime `margin_usage` | regime targets and tail-harvest bands |

The wheel and regime multipliers resolve independently. Each strategy-specific
`risk.margin_usage` overrides `runtime.account.margin_usage`; otherwise the
account setting is the fallback.

For `net_liq_ex_options`, the regime calculation is:

```text
adjusted NLV = NLV - state-owned tail-option value - active-regime option value
regime base = floor(adjusted NLV * regime margin_usage)
```

For `net_liq`, only the state-owned tail-option value is removed. For
`managed_stocks`, the base is the marked value of active regime stocks and no
margin multiplier is applied.

Configured portfolio weights always sum to 100%. Volatility-adjusted regime
weights remain absolute rather than being renormalized, so an increase from a
configured 60%/40% allocation to 65%/40% targets 105% of the NLV-backed regime
base. That 5% excess is intentional stacked exposure; the deficit rail measures
only exposure beyond the resulting 105% gross target. `managed_stocks` does not
support stacking because using the changing sleeve value as the base would
compound an above-100% target on subsequent runs.

Every `CapitalBase` retains its gross value, exclusions, selected multiplier,
and final value so logs and telemetry can show how the number was derived.

## Position taxonomy

`PositionCategory` splits market value into non-overlapping buckets:

- active regime stocks;
- other configured portfolio stocks;
- the configured cash fund;
- state-owned tail options;
- other options on active regime symbols;
- unrelated options; and
- other assets.

State ownership is quantity-aware. If one live option position contains both a
ThetaGang-owned quantity and an additional manual quantity, the market value is
split proportionally. Only the state-owned portion is treated as a tail hedge.
The remaining portion is excluded only when the contract is also an option on
an active regime symbol. This prevents ownership state from silently claiming
an entire broker position.

## Cash ledger

`CashLedger` applies cash-management inputs in a fixed order:

```text
after pending debits = settled cash - pending debits
sweepable cash = after pending debits - reserved regime cash
projected cash = after pending debits + pending credits
```

Only sweepable cash can fund a cash-fund purchase. Pending credits may prevent a
duplicate cash-fund liquidation, but they cannot fund a purchase before they
settle. Any order whose remaining cash notional cannot be priced makes the
ledger ambiguous and cash management fails closed.

Order notional and pending debit/credit accounting also live in the accounting
module. They remain re-exported from `thetagang.orders` for compatibility, but
new accounting consumers should import them from `thetagang.accounting`.

## Development rule

New code that needs NLV, strategy buying power, portfolio exclusions, stock
exposure, or available cash should extend the accounting taxonomy and its
invariant tests. It should not index `AccountValue` dictionaries or reproduce a
capital formula inside a strategy engine.
