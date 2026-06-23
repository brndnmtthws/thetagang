from typing import Any, Dict, Optional

from ib_async import Ticker

from thetagang.config import MINIMUM_PRICE, Config


def _ticker(
    *,
    bid: float = float("nan"),
    ask: float = float("nan"),
    last: float = float("nan"),
) -> Ticker:
    # Ticker price fields must be assigned as attributes (constructor kwargs are
    # ignored). Ticker.midpoint() is derived from bid/ask, so tests that need a
    # specific midpoint set bid == ask to that value.
    ticker = Ticker()
    ticker.bid = bid
    ticker.ask = ask
    ticker.last = last
    # midpoint() requires positive bid/ask sizes (Ticker.hasBidAsk()).
    ticker.bidSize = 1
    ticker.askSize = 1
    return ticker


def _config(
    *,
    symbols: Optional[Dict[str, Any]] = None,
    orders: Optional[Dict[str, Any]] = None,
) -> Config:
    cfg: Dict[str, Any] = {
        "meta": {"schema_version": 2},
        "run": {"strategies": ["wheel"]},
        "runtime": {
            "account": {"number": "DUX", "margin_usage": 0.5},
            "option_chains": {"expirations": 4, "strikes": 10},
        },
        "portfolio": {"symbols": symbols or {"AAA": {"weight": 1.0}}},
        "strategies": {
            "wheel": {
                "defaults": {
                    "target": {"dte": 30, "minimum_open_interest": 5},
                    "roll_when": {"dte": 7},
                }
            }
        },
    }
    if orders is not None:
        cfg["runtime"]["orders"] = orders
    return Config(**cfg)


def test_unset_preserves_fallback() -> None:
    config = _config()
    ticker = _ticker(bid=9.99, ask=9.99)
    # Nothing configured -> the caller's existing price is returned unchanged.
    assert config.get_order_limit_price("AAA", ticker, "SELL", 2.5) == 2.5
    assert config.get_order_limit_price("AAA", ticker, "BUY", 1.25) == 1.25


def test_unknown_symbol_preserves_fallback() -> None:
    config = _config()
    ticker = _ticker(ask=9.99)
    assert config.get_order_limit_price("VIX", ticker, "SELL", 3.0) == 3.0


def test_symbol_sell_price_uses_ask() -> None:
    config = _config(symbols={"AAA": {"weight": 1.0, "sell_price": "ask"}})
    ticker = _ticker(bid=2.90, ask=3.10)
    assert config.get_order_limit_price("AAA", ticker, "SELL", 2.0) == 3.10


def test_symbol_buy_price_uses_bid() -> None:
    config = _config(symbols={"AAA": {"weight": 1.0, "buy_price": "bid"}})
    ticker = _ticker(bid=2.90, ask=3.10)
    assert config.get_order_limit_price("AAA", ticker, "BUY", 5.0) == 2.90


def test_price_adjustment_is_applied() -> None:
    config = _config(
        symbols={
            "AAA": {"weight": 1.0, "sell_price": "ask", "sell_price_adjustment": -0.05}
        }
    )
    ticker = _ticker(ask=3.00)
    assert config.get_order_limit_price("AAA", ticker, "SELL", 2.0) == 2.95


def test_adjustment_cannot_push_below_minimum_price() -> None:
    config = _config(
        symbols={
            "AAA": {"weight": 1.0, "sell_price": "ask", "sell_price_adjustment": -1.0}
        }
    )
    ticker = _ticker(ask=0.50)
    assert config.get_order_limit_price("AAA", ticker, "SELL", 2.0) == MINIMUM_PRICE


def test_nan_chosen_price_falls_back() -> None:
    config = _config(symbols={"AAA": {"weight": 1.0, "sell_price": "bid"}})
    ticker = _ticker()  # bid is NaN
    assert config.get_order_limit_price("AAA", ticker, "SELL", 4.0) == 4.0


def test_latest_falls_back_to_midpoint_when_unavailable() -> None:
    config = _config(symbols={"AAA": {"weight": 1.0, "sell_price": "latest"}})
    ticker = _ticker(bid=2.2, ask=2.2)  # last is NaN -> midpoint == 2.2
    assert config.get_order_limit_price("AAA", ticker, "SELL", 9.0) == 2.2


def test_global_default_applies_when_symbol_unset() -> None:
    config = _config(orders={"sell_price": "ask"})
    ticker = _ticker(ask=3.30)
    assert config.get_order_limit_price("AAA", ticker, "SELL", 2.0) == 3.30


def test_symbol_overrides_global_default() -> None:
    config = _config(
        symbols={"AAA": {"weight": 1.0, "sell_price": "midpoint"}},
        orders={"sell_price": "ask"},
    )
    ticker = _ticker(bid=0.70, ask=3.30)  # midpoint == 2.0
    assert config.get_order_limit_price("AAA", ticker, "SELL", 9.0) == 2.0


def test_global_price_adjustment_applies() -> None:
    config = _config(orders={"sell_price": "ask", "sell_price_adjustment": -0.10})
    ticker = _ticker(ask=3.00)
    assert config.get_order_limit_price("AAA", ticker, "SELL", 2.0) == 2.90
