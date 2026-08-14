# Tail hedging

ThetaGang can maintain a small ladder of long downside puts. The goal is cheap,
recurring convexity within a fixed annual budget, not continuous coverage or a
crash forecast. When VIX or premiums are high, waiting is intentional: buying
expensive insurance defeats the strategy.

## How it works

For each configured target, the tail stage:

1. Reconciles state-owned contracts with current IBKR positions and working
   orders.
2. Closes puts at or inside `exit_dte`, and closes puts whose target was removed.
3. Applies minimum entry spacing, the annual budget, optional VIX gate, and
   quote filters.
4. Chooses an expiration near `target_dte` and a strike near
   `spot * strike_ratio`, separated from the latest existing tranche by the
   same minimum.
5. Buys only the whole-contract quantity that fits every budget limit.

Targets are independent. A blocked entry or quote failure for one symbol does
not stop the others.

### Budget and minimum spacing

`annual_budget` caps gross entry debit across the program over a rolling 365
days. Target `budget_weight` values must sum to `1.0`.

```text
global budget = current NLV * annual_budget
target budget = global budget * budget_weight
tranche budget = target budget / annual_tranches
applicable budget = min(global remaining, target remaining, tranche budget)
quantity = floor(applicable budget / contract cost)
```

No order is placed when one contract does not fit. Selling or losing a put does
not refund its entry cost: the cap measures insurance purchased, not net profit.
Each target permits at most `annual_tranches` entries in a rolling year.
`365 // annual_tranches` is a hard minimum between entries, not a schedule:
blocked or missed windows never accumulate, and the strategy never catches up
with bunched purchases. A new expiration must also be at least that many days
after the latest live tranche expiration, preventing maturity bunching when the
scanner moves within the configured DTE range.

An entry requires a positive protected-stock position, trading enabled for the
symbol, no same-run stock rebalance or tail close, a later expiration, adequate
bid and open interest, and acceptable spread and premium ratios. The scanner
excludes contracts already held, queued, or working.

The VIX gate may create coverage gaps. Above `entry_vix_max`, the strategy waits
instead of overpaying. `entry_gate = "none"` disables only that gate; all other
filters still apply.

## Crash harvesting

Harvesting is subordinate to the ordinary volatility-adjusted rebalance. A put
may be sold only when its protected symbol has an approved hard-underweight
stock buy, volatility sizing succeeded, and ordinary funding cannot cover the
buy. The put must be an active state-owned long position, have no conflicting
order, and have a fresh sell quote above IBKR average cost. When that cost is
unavailable, the recorded entry limit times the live contract multiplier is the
fallback basis.

Ordinary funding is calculated first:

```text
ordinary capacity = max(0,
    TotalCashValue
    - configured cash target
    - queued BUY debits
    + approved stock SELL value
    + usable cash-ETF market value
    + cash-management sell-threshold tolerance
)
shortfall = max(0, approved stock BUY value - ordinary capacity)
```

Cash-ETF value and sell-threshold tolerance count only when cash management is
enabled and its stage will run later in the same run. This is its fixed point:
retained buys either cause the fund sale or leave cash within configured
hysteresis. Usable fund value is limited to whole shares at a finite live price;
approved fund sales reduce that value so the same holding is not counted twice.
If the stage is omitted, both terms are zero; the configured cash target remains
reserved.

Shortfall is allocated among eligible buys in proportion to their dollar value,
then rounded to whole shares with a deterministic largest-remainder allocation.
Profitable puts are considered by shortest expiration. A tranche too small to
fund one deferred share is preserved and the next useful tranche is considered.
Each target monetizes at most one state tranche per run, selling the fewest whole
contracts available toward its allocation.

```text
ordinary shares now = approved shares - deferred shares
```

All allocated unfunded shares are deferred once a sale is queued or already
working, even when that tranche can finance only part of them. Ordinary-funded
shares remain in the current run. Put sales use
`tg:tail-harvest:<symbol>:<conId>` order references. Remaining tranches stay
invested for later runs if the drawdown and shortfall continue.

There is no harvest plan, estimated-credit ledger, commission ledger, or custom
tail-funded stock order. Estimated tail-reduction proceeds are excluded from
same-run cash management. After an actual fill, IBKR reports the proceeds in
`TotalCashValue`; a later ordinary rebalance spends that cash before another
tranche is considered. A working harvest order prevents another sale for that
symbol and the still-unfunded stock shares remain deferred.

Profitability is gross of commission. Whole-contract sizing may realize more
cash than the deferred stock notional; excess proceeds remain ordinary cash.

## State and safety

File-backed SQLite is required because an IBKR position does not identify its
owning strategy. The account-scoped `tail_hedge_state` event stores only exact
owned tranches and rolling gross entry history. Ownership is persisted before a
risk-increasing BUY is queued. It follows the account across config-file renames
and is never shared with another account.

Working orders are read from IBKR by account and order reference. Before
submission, closes are capped by the live long position minus working and
earlier same-run close commitments, preventing a stale snapshot from creating a
short put. Harvesting and cash management re-read ib_async's synchronized
account and position caches after quote awaits; they do not issue recurring
refresh requests. Cash management does not stack another order on a working or
same-run cash-ETF order.

Dry-run state is visible within that run but is never reused as live state, and
dry runs never cancel broker orders.

Disabling the strategy while retaining targets freezes the ladder: no tail
orders are created, but SQLite remains required so wheel and allocation logic
can still recognize owned puts. To retire a target, keep the strategy enabled,
remove that target, and let the tail stage close it. Disable the strategy and
remove its target list only after the positions and working orders are gone.

## Other strategies

- `net_liq_ex_options` excludes all options from the regime allocation base.
  `net_liq` excludes exact state-owned tail puts. `managed_stocks` uses managed
  stock value only.
- Cash and a usable cash ETF fund rebalancing before tail puts. Unfilled tail
  reductions never count as pending cash.
- Wheel put counts, management, rolls, and scans exclude exact state-owned tail
  contracts. If ownership cannot be read, put-side wheel actions stop.

## Configuration

Tail entries and DTE exits require the tail strategy. Harvesting additionally
requires regime rebalancing for the same symbol. Tail hedging is incompatible
with `regime_rebalance.shares_only = true`.

```toml
[run]
strategies = ["regime_rebalance", "tail_hedge", "cash_management"]

[runtime.database]
enabled = true
path = "data/thetagang.db"

[strategies.cash_management]
enabled = true
cash_fund = "SGOV"
target_cash_balance = 0

[strategies.tail_hedge]
enabled = true
annual_budget = 0.005

[[strategies.tail_hedge.targets]]
symbol = "QQQ"
budget_weight = 1.0
annual_tranches = 4
entry_gate = "vix"
entry_vix_max = 20.0
target_dte = 180
min_dte = 150
max_dte = 210
exit_dte = 30
strike_ratio = 0.60
minimum_open_interest = 50
minimum_bid = 0.01
max_bid_ask_ratio = 0.50
max_premium_ratio = 0.05
```

These values show the configuration shape, not a calibrated recommendation.
The strategy is disabled by default. Small accounts, high VIX, expensive or
illiquid contracts, minimum spacing, budget exhaustion, or a missing later
expiration can all create acceptable coverage gaps. Avoid manually trading a
state-owned
tail contract because IBKR nets manual and strategy positions together.
