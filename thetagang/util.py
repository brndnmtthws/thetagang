import math
from datetime import datetime

from ib_insync import TagValue, util
from ib_insync.contract import Option

from thetagang.options import option_dte


def account_summary_to_dict(account_summary):
    d = dict()
    for s in account_summary:
        d[s.tag] = s
    return d


def portfolio_positions_to_dict(portfolio_positions):
    d = dict()
    for p in portfolio_positions:
        symbol = p.contract.symbol
        if symbol not in d:
            d[symbol] = []
        d[symbol].append(p)
    return d


def position_pnl(position):
    return position.unrealizedPNL / abs(position.averageCost * position.position)


def count_short_option_positions(symbol, portfolio_positions, right):
    if symbol in portfolio_positions:
        return math.floor(
            -sum(
                [
                    p.position
                    for p in portfolio_positions[symbol]
                    if isinstance(p.contract, Option)
                    and p.contract.right.upper().startswith(right.upper())
                    and p.position < 0
                ]
            )
        )

    return 0


def count_long_option_positions(symbol, portfolio_positions, right):
    if symbol in portfolio_positions:
        return math.floor(
            sum(
                [
                    p.position
                    for p in portfolio_positions[symbol]
                    if isinstance(p.contract, Option)
                    and p.contract.right.upper().startswith(right.upper())
                    and p.position > 0
                ]
            )
        )

    return 0


def net_option_positions(symbol, portfolio_positions, right, ignore_zero_dte=False):
    if symbol in portfolio_positions:
        return math.floor(
            sum(
                [
                    p.position
                    for p in portfolio_positions[symbol]
                    if isinstance(p.contract, Option)
                    and p.contract.right.upper().startswith(right.upper())
                    and option_dte(p.contract.lastTradeDateOrContractMonth) >= 0
                    and (
                        not ignore_zero_dte
                        or option_dte(p.contract.lastTradeDateOrContractMonth) > 0
                    )
                ]
            )
        )

    return 0


def wait_n_seconds(pred, body, seconds_to_wait, started_at=None):
    if not started_at:
        started_at = datetime.now()
    diff = datetime.now() - started_at
    remaining = seconds_to_wait - diff.seconds
    if not remaining or remaining <= 0 or math.isclose(remaining, 0.0):
        raise RuntimeError(
            "Exhausted retries waiting on predicate. This shouldn't happen."
        )
    if pred():
        body(remaining)
        wait_n_seconds(pred, body, seconds_to_wait, started_at)


def get_higher_price(ticker):
    # Returns the highest of either the option model price, the midpoint, or the
    # market price. The midpoint is usually a bit higher than the IB model's
    # pricing, but we want to avoid leaving money on the table in cases where
    # the spread might be messed up. This may in some cases make it harder for
    # orders to fill in a given day, but I think that's a reasonable tradeoff to
    # avoid leaving money on the table.
    if ticker.modelGreeks:
        return max([midpoint_or_market_price(ticker), ticker.modelGreeks.optPrice])
    return midpoint_or_market_price(ticker)


def get_lower_price(ticker):
    # Same as get_highest_price(), except get the lower price instead.
    if ticker.modelGreeks:
        return min([midpoint_or_market_price(ticker), ticker.modelGreeks.optPrice])
    return midpoint_or_market_price(ticker)


def midpoint_or_market_price(ticker):
    # As per the ib_insync docs, marketPrice returns the last price first, but
    # we often prefer the midpoint over the last price. This function pulls the
    # midpoint first, then falls back to marketPrice() if midpoint is nan.
    if util.isNan(ticker.midpoint()):
        if util.isNan(ticker.marketPrice()) and ticker.modelGreeks:
            # Fallback to the model price if the greeks are available
            return ticker.modelGreeks.optPrice
        else:
            return ticker.marketPrice()

    return ticker.midpoint()


def get_target_delta(config, symbol, right):
    p_or_c = "calls" if right.upper().startswith("C") else "puts"
    if (
        p_or_c in config["symbols"][symbol]
        and "delta" in config["symbols"][symbol][p_or_c]
    ):
        return config["symbols"][symbol][p_or_c]["delta"]
    if "delta" in config["symbols"][symbol]:
        return config["symbols"][symbol]["delta"]
    if p_or_c in config["target"]:
        return config["target"][p_or_c]["delta"]
    return config["target"]["delta"]


def get_strike_limit(config, symbol, right):
    p_or_c = "calls" if right.upper().startswith("C") else "puts"
    if (
        p_or_c in config["symbols"][symbol]
        and "strike_limit" in config["symbols"][symbol][p_or_c]
    ):
        return config["symbols"][symbol][p_or_c]["strike_limit"]
    return None


def get_call_cap(config):
    if (
        "write_when" in config
        and "calls" in config["write_when"]
        and "cap_factor" in config["write_when"]["calls"]
    ):
        return max([0, min([1.0, config["write_when"]["calls"]["cap_factor"]])])
    return 1.0


def get_write_threshold(config, symbol, right):
    p_or_c = "calls" if right.upper().startswith("C") else "puts"
    if (
        p_or_c in config["symbols"][symbol]
        and "write_threshold" in config["symbols"][symbol][p_or_c]
    ):
        return config["symbols"][symbol][p_or_c]["write_threshold"]
    if "write_threshold" in config["symbols"][symbol]:
        return config["symbols"][symbol]["write_threshold"]
    return 0.0


def algo_params_from(params):
    return [TagValue(p[0], p[1]) for p in params]
