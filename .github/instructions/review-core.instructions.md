---
applyTo: "thetagang/portfolio_manager.py,thetagang/ibkr.py,thetagang/orders.py,thetagang/trades.py,thetagang/strategies/**/*.py,thetagang/thetagang.py"
---
# Core Trading Review Focus

When reviewing these files:

- Treat changes as high risk by default because they can affect live order behavior.
- Flag any path where `dry_run` could be bypassed or where order placement side effects can happen earlier than before.
- Verify account/portfolio/ticker fetches still handle timeout, partial data, and fallback behavior deterministically.
- Verify decision math is stable for edge values (NaN, zero quantity, low buying power, missing market data).
- Verify regime-rebalance and buy-only/sell-only logic still enforces thresholds and does not flip action direction.
- Require targeted async tests for new branches in execution flow.
- Call out missing assertions for order quantity, side (`BUY`/`SELL`), and price-selection behavior when logic changes.
