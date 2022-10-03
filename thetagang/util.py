import math
from datetime import datetime

from ib_insync import util
from ib_insync.contract import Option


def to_camel_case(snake_str):
    components = snake_str.split("_")
    # We capitalize the first letter of each component except the first one
    # with the 'title' method and join them together.
    return components[0] + "".join(x.title() for x in components[1:])


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
                    and p.contract.right.startswith(right)
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
                    and p.contract.right.startswith(right)
                    and p.position > 0
                ]
            )
        )

    return 0


def wait_n_seconds(pred, body, seconds_to_wait, started_at=None):
    if not started_at:
        started_at = datetime.now()
    diff = datetime.now() - started_at
    remaining = seconds_to_wait - diff.seconds
    if remaining <= 0:
        raise RuntimeError(
            "Exhausted retries waiting on predicate. This shouldn't happen."
        )
    if pred():
        body(remaining)
        wait_n_seconds(pred, body, seconds_to_wait, started_at)


def get_highest_price(ticker):
    # Returns the highest of either the option model price, the midpoint, or the
    # market price. The midpoint is usually a bit higher than the IB model's
    # pricing, but we want to avoid leaving money on the table in cases where
    # the spread might be messed up. This may in some cases make it harder for
    # orders to fill in a given day, but I think that's a reasonable tradeoff to
    # avoid leaving money on the table.
    return max([midpoint_or_market_price(ticker), ticker.modelGreeks.optPrice])


def get_lowest_price(ticker):
    # Same as get_highest_price(), except get the lower price instead.
    return min([midpoint_or_market_price(ticker), ticker.modelGreeks.optPrice])


def midpoint_or_market_price(ticker):
    # As per the ib_insync docs, marketPrice returns the last price first, but
    # we often prefer the midpoint over the last price. This function pulls the
    # midpoint first, then falls back to marketPrice() if midpoint is nan.
    if util.isNan(ticker.midpoint()):
        if util.isNan(ticker.marketPrice()):
            # Fallback to the model price
            return ticker.modelGreeks.optPrice
        else:
            return ticker.marketPrice()

    return ticker.midpoint()


def get_target_delta(config, symbol, right):
    p_or_c = "calls" if right.startswith("C") else "puts"
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
    p_or_c = "calls" if right.startswith("C") else "puts"
    if (
        p_or_c in config["symbols"][symbol]
        and "strike_limit" in config["symbols"][symbol][p_or_c]
    ):
        return config["symbols"][symbol][p_or_c]["strike_limit"]
    return None
