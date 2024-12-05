import math
from operator import itemgetter
from typing import Dict, List, Optional, Tuple

import ib_async.objects
import ib_async.ticker
from ib_async import AccountValue, Order, PortfolioItem, TagValue, Ticker, util
from ib_async.contract import Option

from thetagang.config import Config
from thetagang.options import option_dte


def account_summary_to_dict(
    account_summary: List[AccountValue],
) -> Dict[str, AccountValue]:
    d: Dict[str, AccountValue] = dict()
    for s in account_summary:
        d[s.tag] = s
    return d


def portfolio_positions_to_dict(
    portfolio_positions: List[PortfolioItem],
) -> Dict[str, List[PortfolioItem]]:
    d: Dict[str, List[PortfolioItem]] = dict()
    for p in portfolio_positions:
        symbol = p.contract.symbol
        if symbol not in d:
            d[symbol] = []
        d[symbol].append(p)
    return d


def position_pnl(position: ib_async.objects.PortfolioItem) -> float:
    return position.unrealizedPNL / abs(position.averageCost * position.position)


def get_short_positions(
    positions: List[PortfolioItem], right: str
) -> List[PortfolioItem]:
    return [
        p
        for p in positions
        if isinstance(p.contract, Option)
        and p.contract.right.upper().startswith(right.upper())
        and p.position < 0
    ]


def get_long_positions(
    positions: List[PortfolioItem], right: str
) -> List[PortfolioItem]:
    return [
        p
        for p in positions
        if isinstance(p.contract, Option)
        and p.contract.right.upper().startswith(right.upper())
        and p.position > 0
    ]


def count_short_option_positions(positions: List[PortfolioItem], right: str) -> int:
    return math.floor(-sum([p.position for p in get_short_positions(positions, right)]))


def weighted_avg_short_strike(
    positions: List[PortfolioItem], right: str
) -> Optional[float]:
    shorts = [
        (abs(p.position), p.contract.strike)
        for p in get_short_positions(positions, right)
    ]
    num = sum([p[0] * p[1] for p in shorts])
    den = sum([p[0] for p in shorts])
    if den > 0:
        return num / den


def weighted_avg_long_strike(
    positions: List[PortfolioItem], right: str
) -> Optional[float]:
    shorts = [
        (abs(p.position), p.contract.strike)
        for p in get_long_positions(positions, right)
    ]
    num = sum([p[0] * p[1] for p in shorts])
    den = sum([p[0] for p in shorts])
    if den > 0:
        return num / den


def count_long_option_positions(positions: List[PortfolioItem], right: str) -> int:
    return math.floor(sum([p.position for p in get_long_positions(positions, right)]))


def calculate_net_short_positions(positions: List[PortfolioItem], right: str) -> int:
    shorts = [
        (
            option_dte(p.contract.lastTradeDateOrContractMonth),
            p.contract.strike,
            p.position,
        )
        for p in get_short_positions(positions, right)
    ]
    longs = [
        (
            option_dte(p.contract.lastTradeDateOrContractMonth),
            p.contract.strike,
            p.position,
        )
        for p in get_long_positions(positions, right)
    ]
    shorts = sorted(shorts, key=itemgetter(0, 1), reverse=right.upper().startswith("P"))
    longs = sorted(longs, key=itemgetter(0, 1), reverse=right.upper().startswith("P"))

    def calc_net(short_dte: int, short_strike: float, short_position: float) -> float:
        for i in range(len(longs)):
            if short_position > -1:
                break
            (long_dte, long_strike, long_position) = longs[i]
            if long_position < 1:
                # ignore empty long positions
                continue
            if long_dte >= short_dte:
                if (
                    math.isclose(short_strike, long_strike)
                    or (right.upper().startswith("P") and long_strike >= short_strike)
                    or (right.upper().startswith("C") and long_strike <= short_strike)
                ):
                    if short_position + long_position > 0:
                        long_position = short_position + long_position
                        short_position = 0
                    else:
                        short_position += long_position
                        long_position = 0
            longs[i] = (long_dte, long_strike, long_position)
        return min([0.0, short_position])

    nets = [calc_net(*short) for short in shorts]

    return math.floor(-sum(nets))


def net_option_positions(
    symbol: str,
    portfolio_positions: Dict[str, List[PortfolioItem]],
    right: str,
    ignore_dte: Optional[int] = None,
) -> int:
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
    # As per the ib_async docs, marketPrice returns the last price first, but
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
            return ticker.marketPrice() if not util.isNan(ticker.marketPrice()) else 0.0

    return ticker.midpoint()


def get_target_dte(config: Config, symbol: str) -> int:
    symbol_config = config.symbols.get(symbol)
    return (
        symbol_config.dte
        if symbol_config and symbol_config.dte is not None
        else config.target.dte
    )


def get_target_delta(config: Config, symbol: str, right: str) -> float:
    p_or_c = "calls" if right.upper().startswith("C") else "puts"
    symbol_config = config.symbols.get(symbol)

    if symbol_config:
        option_config = getattr(symbol_config, p_or_c, None)
        if option_config and option_config.delta is not None:
            return option_config.delta
        if symbol_config.delta is not None:
            return symbol_config.delta

    target_option = getattr(config.target, p_or_c, None)
    if target_option and target_option.delta is not None:
        return target_option.delta

    return config.target.delta


def get_cap_factor(config: Config, symbol: str) -> float:
    symbol_config = config.symbols.get(symbol)
    if (
        symbol_config
        and symbol_config.calls
        and symbol_config.calls.cap_factor is not None
    ):
        return symbol_config.calls.cap_factor
    return config.write_when.calls.cap_factor


def get_cap_target_floor(config: Config, symbol: str) -> float:
    symbol_config = config.symbols.get(symbol)
    if (
        symbol_config
        and symbol_config.calls
        and symbol_config.calls.cap_target_floor is not None
    ):
        return symbol_config.calls.cap_target_floor
    return config.write_when.calls.cap_target_floor


def get_strike_limit(config: Config, symbol: str, right: str) -> Optional[float]:
    p_or_c = "calls" if right.upper().startswith("C") else "puts"
    symbol_config = config.symbols.get(symbol)
    option_config = getattr(symbol_config, p_or_c, None) if symbol_config else None
    return option_config.strike_limit if option_config else None


def get_target_calls(
    config: Config, symbol: str, current_shares: int, target_shares: int
) -> int:
    if write_excess_calls_only(config, symbol):
        return max([0, (current_shares - target_shares) // 100])
    else:
        cap_factor = get_cap_factor(config, symbol)
        cap_target_floor = get_cap_target_floor(config, symbol)
        min_uncovered = (target_shares * cap_target_floor) // 100
        max_covered = (current_shares * cap_factor) // 100
        total_coverable = current_shares // 100

        return max([0, math.floor(min([max_covered, total_coverable - min_uncovered]))])


def get_write_threshold_sigma(
    config: Config, symbol: Optional[str], right: str
) -> Optional[float]:
    p_or_c = "calls" if right.upper().startswith("C") else "puts"

    if symbol:
        symbol_config = config.symbols.get(symbol)
        if symbol_config:
            option_config = getattr(symbol_config, p_or_c, None)
            if option_config:
                if option_config.write_threshold_sigma is not None:
                    return option_config.write_threshold_sigma
                if option_config.write_threshold is not None:
                    return None

            if symbol_config.write_threshold_sigma is not None:
                return symbol_config.write_threshold_sigma
            if symbol_config.write_threshold is not None:
                return None

    if config.constants:
        option_constants = getattr(config.constants, p_or_c, None)
        if option_constants and option_constants.write_threshold_sigma is not None:
            return option_constants.write_threshold_sigma
        if config.constants.write_threshold_sigma is not None:
            return config.constants.write_threshold_sigma

    return None


def get_write_threshold_perc(
    config: Config, symbol: Optional[str], right: str
) -> float:
    p_or_c = "calls" if right.upper().startswith("C") else "puts"

    if symbol:
        symbol_config = config.symbols.get(symbol)
        if symbol_config:
            option_config = getattr(symbol_config, p_or_c, None)
            if option_config and option_config.write_threshold is not None:
                return option_config.write_threshold
            if symbol_config.write_threshold is not None:
                return symbol_config.write_threshold

    if config.constants:
        option_constants = getattr(config.constants, p_or_c, None)
        if option_constants and option_constants.write_threshold is not None:
            return option_constants.write_threshold
        if config.constants.write_threshold is not None:
            return config.constants.write_threshold

    return 0.0


def algo_params_from(params: List[List[str]]) -> List[TagValue]:
    return [TagValue(p[0], p[1]) for p in params]


def get_minimum_credit(config: Config) -> float:
    return config.orders.minimum_credit


def maintain_high_water_mark(config: Config, symbol: str) -> bool:
    symbol_config = config.symbols.get(symbol)
    if (
        symbol_config
        and symbol_config.calls
        and symbol_config.calls.maintain_high_water_mark is not None
    ):
        return symbol_config.calls.maintain_high_water_mark
    return config.roll_when.calls.maintain_high_water_mark


def get_max_dte_for(symbol: str, config: Config) -> Optional[int]:
    if symbol == "VIX" and config.vix_call_hedge.max_dte is not None:
        return config.vix_call_hedge.max_dte

    symbol_config = config.symbols.get(symbol)
    if symbol_config and symbol_config.max_dte is not None:
        return symbol_config.max_dte

    return config.target.max_dte


def would_increase_spread(order: Order, updated_price: float) -> bool:
    return (
        order.action == "BUY"
        and updated_price < order.lmtPrice
        or order.action == "SELL"
        and updated_price > order.lmtPrice
    )


def can_write_when(config: Config, symbol: str, right: str) -> Tuple[bool, bool]:
    p_or_c = "calls" if right.upper().startswith("C") else "puts"
    symbol_config = config.symbols.get(symbol)
    option_config = getattr(symbol_config, p_or_c, None) if symbol_config else None
    default_config = getattr(config.write_when, p_or_c)

    can_write_when_green = (
        option_config.write_when.green
        if option_config and option_config.write_when
        else default_config.green
    )
    can_write_when_red = (
        option_config.write_when.red
        if option_config is not None and option_config.write_when is not None
        else default_config.red
    )

    return (can_write_when_green, can_write_when_red)


def close_if_unable_to_roll(config: Config, symbol: str) -> bool:
    symbol_config = config.symbols.get(symbol)
    return (
        symbol_config.close_if_unable_to_roll
        if symbol_config and symbol_config.close_if_unable_to_roll is not None
        else config.roll_when.close_if_unable_to_roll
    )


def trading_is_allowed(config: Config, symbol: str) -> bool:
    symbol_config = config.symbols.get(symbol)
    return not symbol_config or not symbol_config.no_trading


def write_excess_calls_only(config: Config, symbol: str) -> bool:
    symbol_config = config.symbols.get(symbol)
    if (
        symbol_config
        and symbol_config.calls
        and symbol_config.calls.excess_only is not None
    ):
        return symbol_config.calls.excess_only
    return config.write_when.calls.excess_only
