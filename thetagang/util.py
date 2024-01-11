import math
from datetime import datetime
from typing import Optional

from ib_insync import PortfolioItem, TagValue, Ticker, util
from ib_insync.contract import Option

from thetagang.options import option_dte


def account_summary_to_dict(account_summary):
    d = dict()
    for s in account_summary:
        d[s.tag] = s
    return d


def portfolio_positions_to_dict(
    portfolio_positions: list[PortfolioItem],
) -> dict[str, list[PortfolioItem]]:
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


def net_option_positions(symbol, portfolio_positions, right, ignore_dte=None):
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
                        not ignore_dte
                        or option_dte(p.contract.lastTradeDateOrContractMonth)
                        > ignore_dte
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


def get_higher_price(ticker: Ticker) -> float:
    # Returns the highest of either the option model price, the midpoint, or the
    # market price. The midpoint is usually a bit higher than the IB model's
    # pricing, but we want to avoid leaving money on the table in cases where
    # the spread might be messed up. This may in some cases make it harder for
    # orders to fill in a given day, but I think that's a reasonable tradeoff to
    # avoid leaving money on the table.
    if ticker.modelGreeks and ticker.modelGreeks.optPrice:
        return max([midpoint_or_market_price(ticker), ticker.modelGreeks.optPrice])
    return midpoint_or_market_price(ticker)


def get_lower_price(ticker: Ticker) -> float:
    # Same as get_highest_price(), except get the lower price instead.
    if ticker.modelGreeks and ticker.modelGreeks.optPrice:
        return min([midpoint_or_market_price(ticker), ticker.modelGreeks.optPrice])
    return midpoint_or_market_price(ticker)


def midpoint_or_market_price(ticker: Ticker) -> float:
    # As per the ib_insync docs, marketPrice returns the last price first, but
    # we often prefer the midpoint over the last price. This function pulls the
    # midpoint first, then falls back to marketPrice() if midpoint is nan.
    if util.isNan(ticker.midpoint()):
        if (
            util.isNan(ticker.marketPrice())
            and ticker.modelGreeks
            and ticker.modelGreeks.optPrice
        ):
            # Fallback to the model price if the greeks are available
            return ticker.modelGreeks.optPrice
        else:
            return ticker.marketPrice()

    return ticker.midpoint()


def get_target_delta(config: dict, symbol: str, right: str):
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


def get_cap_factor(config: dict, symbol: str):
    if (
        "calls" in config["symbols"][symbol]
        and "cap_factor" in config["symbols"][symbol]["calls"]
    ):
        return config["symbols"][symbol]["calls"]["cap_factor"]
    return config["write_when"]["calls"]["cap_factor"]


def get_cap_target_floor(config: dict, symbol: str):
    if (
        "calls" in config["symbols"][symbol]
        and "cap_target_floor" in config["symbols"][symbol]["calls"]
    ):
        return config["symbols"][symbol]["calls"]["cap_target_floor"]
    return config["write_when"]["calls"]["cap_target_floor"]


def get_strike_limit(config: dict, symbol: str, right: str) -> Optional[float]:
    p_or_c = "calls" if right.upper().startswith("C") else "puts"
    if (
        p_or_c in config["symbols"][symbol]
        and "strike_limit" in config["symbols"][symbol][p_or_c]
    ):
        return config["symbols"][symbol][p_or_c]["strike_limit"]
    return None


def get_target_calls(
    config: dict, symbol: str, current_shares: int, target_shares: int
) -> int:
    cap_factor = get_cap_factor(config, symbol)
    cap_target_floor = get_cap_target_floor(config, symbol)
    min_uncovered = (target_shares * cap_target_floor) // 100
    max_covered = (current_shares * cap_factor) // 100
    total_coverable = current_shares // 100

    return max([0, math.floor(min([max_covered, total_coverable - min_uncovered]))])


def get_write_threshold_sigma(
    config: dict, symbol: Optional[str], right: str
) -> Optional[float]:
    p_or_c = "calls" if right.upper().startswith("C") else "puts"
    if symbol:
        if (
            p_or_c in config["symbols"][symbol]
            and "write_threshold_sigma" in config["symbols"][symbol][p_or_c]
        ):
            return config["symbols"][symbol][p_or_c]["write_threshold_sigma"]
        if "write_threshold_sigma" in config["symbols"][symbol]:
            return config["symbols"][symbol]["write_threshold_sigma"]
        # if there's a percentage-based threshold defined, we want to use that, so we return None here
        if (
            p_or_c in config["symbols"][symbol]
            and "write_threshold" in config["symbols"][symbol][p_or_c]
        ) or "write_threshold" in config["symbols"][symbol]:
            return None

    # check if there's a default value in constants
    if (
        p_or_c in config["constants"]
        and "write_threshold_sigma" in config["constants"][p_or_c]
    ):
        return config["constants"][p_or_c]["write_threshold_sigma"]
    if "write_threshold_sigma" in config["constants"]:
        return config["constants"]["write_threshold_sigma"]

    return None


def get_write_threshold_perc(config: dict, symbol: Optional[str], right: str) -> float:
    p_or_c = "calls" if right.upper().startswith("C") else "puts"
    if symbol:
        if (
            p_or_c in config["symbols"][symbol]
            and "write_threshold" in config["symbols"][symbol][p_or_c]
        ):
            return config["symbols"][symbol][p_or_c]["write_threshold"]
        if "write_threshold" in config["symbols"][symbol]:
            return config["symbols"][symbol]["write_threshold"]

    # check if there's a default value in constants
    if (
        p_or_c in config["constants"]
        and "write_threshold" in config["constants"][p_or_c]
    ):
        return config["constants"][p_or_c]["write_threshold"]
    if "write_threshold" in config["constants"]:
        return config["constants"]["write_threshold"]
    return 0.0


def algo_params_from(params):
    return [TagValue(p[0], p[1]) for p in params]


def get_minimum_credit(config: dict) -> float:
    return config["orders"].get("minimum_credit", 0.0)


def maintain_high_water_mark(config: dict, symbol: str) -> bool:
    if (
        "calls" in config["symbols"][symbol]
        and "maintain_high_water_mark" in config["symbols"][symbol]["calls"]
    ):
        return config["symbols"][symbol]["calls"]["maintain_high_water_mark"]
    return config["roll_when"]["calls"]["maintain_high_water_mark"]
