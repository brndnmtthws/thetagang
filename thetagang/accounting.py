from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, NamedTuple, TypeAlias

from ib_async import AccountValue, Contract, PortfolioItem
from ib_async.contract import Option, Stock

from thetagang.config_models import RegimeRebalanceBaseEnum

AccountSummary: TypeAlias = dict[str, AccountValue]
PortfolioBySymbol: TypeAlias = dict[str, list[PortfolioItem]]

__all__ = [
    "AccountingError",
    "AccountingPolicy",
    "AccountMetric",
    "AccountSummary",
    "BrokerAccountSnapshot",
    "CapitalBase",
    "CapitalBaseKind",
    "CashLedger",
    "PendingBuyCash",
    "PendingOrderCash",
    "PortfolioAccounting",
    "PortfolioBySymbol",
    "PositionCategory",
    "PositionLedger",
    "RegimeRebalanceBaseEnum",
    "account_summary_from_values",
    "order_cash_notional",
    "owned_option_market_value",
    "pending_buy_cash",
    "pending_order_cash",
    "queued_buy_cash",
    "queued_order_cash",
    "select_account_value",
    "state_owned_option_values",
    "stock_market_value",
    "working_buy_cash",
    "working_order_cash",
]


class AccountingError(RuntimeError):
    """Raised when a portfolio value cannot be accounted for safely."""


class AccountMetric(str, Enum):
    """Broker account values used by ThetaGang's accounting policies."""

    NET_LIQUIDATION = "NetLiquidation"
    TOTAL_CASH = "TotalCashValue"
    EXCESS_LIQUIDITY = "ExcessLiquidity"
    INITIAL_MARGIN = "InitMarginReq"
    MAINTENANCE_MARGIN = "FullMaintMarginReq"
    BROKER_BUYING_POWER = "BuyingPower"
    CUSHION = "Cushion"


class CapitalBaseKind(str, Enum):
    """The intentionally different capital bases used by strategies."""

    NET_LIQUIDATION = "net_liquidation"
    WHEEL_BUYING_POWER = "wheel_buying_power"
    REGIME_REBALANCE = "regime_rebalance"


class PositionCategory(str, Enum):
    """Mutually exclusive economic buckets for portfolio market value."""

    REGIME_STOCK = "regime_stock"
    PORTFOLIO_STOCK = "portfolio_stock"
    CASH_FUND = "cash_fund"
    TAIL_HEDGE_OPTION = "tail_hedge_option"
    REGIME_OPTION = "regime_option"
    OTHER_OPTION = "other_option"
    OTHER_ASSET = "other_asset"


def owned_option_market_value(
    position: PortfolioItem,
    owned_quantity: int,
) -> tuple[int, float] | None:
    """Return the quantity and value attributable to state ownership."""
    if not isinstance(position.contract, Option) or owned_quantity <= 0:
        return None
    try:
        live_quantity = _finite_number(
            position.position,
            description=f"{position.contract.symbol} option quantity",
        )
        reported_value = _finite_number(
            getattr(position, "marketValue", 0.0) or 0.0,
            description=f"{position.contract.symbol} option market value",
        )
    except AccountingError:
        return None
    if live_quantity <= 0:
        return None
    accounted_quantity = min(owned_quantity, math.floor(live_quantity))
    if accounted_quantity <= 0:
        return None
    return accounted_quantity, reported_value * accounted_quantity / live_quantity


def state_owned_option_values(
    portfolio_positions: PortfolioBySymbol,
    owned_quantities: Mapping[int, int],
) -> dict[int, float]:
    """Return market value by contract for the state-owned quantity only."""
    values: dict[int, float] = {}
    for positions in portfolio_positions.values():
        for position in positions:
            con_id = position.contract.conId
            if type(con_id) is not int or con_id not in owned_quantities:
                continue
            owned_value = owned_option_market_value(
                position,
                owned_quantities[con_id],
            )
            if owned_value is not None:
                values[con_id] = owned_value[1]
    return values


def stock_market_value(
    positions: Iterable[PortfolioItem],
    *,
    long_only: bool = False,
) -> float:
    """Return stock exposure with a position-price fallback for missing marks."""
    total = 0.0
    for position in positions:
        if not isinstance(position.contract, Stock):
            continue
        quantity = _finite_number(
            position.position,
            description=f"{position.contract.symbol} stock quantity",
        )
        if long_only and quantity <= 0:
            continue
        value = _finite_number(
            getattr(position, "marketValue", 0.0) or 0.0,
            description=f"{position.contract.symbol} stock market value",
        )
        if value <= 0 and quantity > 0:
            price = _finite_number(
                getattr(position, "marketPrice", 0.0) or 0.0,
                description=f"{position.contract.symbol} stock market price",
            )
            if price > 0:
                value = quantity * price
        total += value
    return total


def _finite_number(value: Any, *, description: str) -> float:
    if isinstance(value, bool):
        raise AccountingError(f"{description} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AccountingError(f"{description} must be a finite number") from exc
    if not math.isfinite(number):
        raise AccountingError(f"{description} must be a finite number")
    return number


def _nonnegative_number(value: Any, *, description: str) -> float:
    number = _finite_number(value, description=description)
    if number < 0:
        raise AccountingError(f"{description} must be non-negative")
    return number


def _account_value_priority(value: AccountValue) -> tuple[int, str, str]:
    currency = str(getattr(value, "currency", "") or "").upper()
    model_code = str(getattr(value, "modelCode", "") or "")
    if currency == "BASE" and not model_code:
        rank = 0
    elif currency == "BASE":
        rank = 1
    else:
        rank = 2
    return rank, currency, model_code


def account_summary_from_values(
    values: Iterable[AccountValue],
) -> dict[str, AccountValue]:
    """Select one deterministic aggregate value for each broker account tag."""
    by_tag: dict[str, list[AccountValue]] = {}
    for value in values:
        tag = str(getattr(value, "tag", "") or "")
        if tag:
            by_tag.setdefault(tag, []).append(value)

    summary: dict[str, AccountValue] = {}
    for tag, candidates in by_tag.items():
        if tag in {metric.value for metric in AccountMetric}:
            candidates = [
                candidate
                for candidate in candidates
                if not isinstance(candidate.value, bool)
                and _is_finite_number(candidate.value)
            ]
            if not candidates:
                continue
        ranked = sorted(candidates, key=_account_value_priority)
        best_priority = _account_value_priority(ranked[0])[0]
        best = [
            candidate
            for candidate in ranked
            if _account_value_priority(candidate)[0] == best_priority
        ]
        if len(best) == 1:
            summary[tag] = best[0]
    return summary


def _is_finite_number(value: Any) -> bool:
    try:
        return not isinstance(value, bool) and math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def select_account_value(
    values: Iterable[AccountValue],
    metric: AccountMetric | str,
) -> float:
    """Return one unambiguous finite broker value, preferring aggregate BASE."""
    tag = metric.value if isinstance(metric, AccountMetric) else metric
    candidates = [value for value in values if getattr(value, "tag", None) == tag]
    ranked: list[tuple[tuple[int, str, str], float]] = []
    for candidate in candidates:
        try:
            number = _finite_number(candidate.value, description=f"{tag} account value")
        except AccountingError:
            continue
        ranked.append((_account_value_priority(candidate), number))
    if not ranked:
        raise AccountingError(f"{tag} account value is unavailable")

    best_rank = min(priority[0] for priority, _number in ranked)
    best = [number for priority, number in ranked if priority[0] == best_rank]
    if len(best) != 1:
        raise AccountingError(f"{tag} account value is unavailable")
    return best[0]


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    """Typed, validated access to a broker account-summary snapshot."""

    summary: Mapping[str, AccountValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", MappingProxyType(dict(self.summary)))

    @classmethod
    def from_values(cls, values: Iterable[AccountValue]) -> BrokerAccountSnapshot:
        return cls(account_summary_from_values(values))

    @classmethod
    def from_balances(
        cls,
        *,
        net_liquidation: float,
        total_cash: float | None = None,
    ) -> BrokerAccountSnapshot:
        values = {
            AccountMetric.NET_LIQUIDATION.value: AccountValue(
                "",
                AccountMetric.NET_LIQUIDATION.value,
                str(net_liquidation),
                "BASE",
                "",
            )
        }
        if total_cash is not None:
            values[AccountMetric.TOTAL_CASH.value] = AccountValue(
                "",
                AccountMetric.TOTAL_CASH.value,
                str(total_cash),
                "BASE",
                "",
            )
        return cls(values)

    def value(self, metric: AccountMetric) -> float:
        raw = self.summary.get(metric.value)
        if raw is None:
            raise AccountingError(f"{metric.value} account value is unavailable")
        return _finite_number(raw.value, description=f"{metric.value} account value")

    @property
    def net_liquidation(self) -> float:
        value = self.value(AccountMetric.NET_LIQUIDATION)
        if value <= 0:
            raise AccountingError("Net liquidation value is unavailable")
        return value

    @property
    def total_cash(self) -> float:
        return self.value(AccountMetric.TOTAL_CASH)

    def allocation(self, weight: float) -> float:
        return self.net_liquidation * _nonnegative_number(
            weight,
            description="Net-liquidation allocation weight",
        )


def _config_value(config: Any, *path: str, default: Any = None) -> Any:
    value = config
    for name in path:
        value = getattr(value, name, None)
        if value is None:
            return default
    return value


def _resolve_margin_usage(config: Any, resolver_name: str) -> float:
    fallback = _nonnegative_number(
        _config_value(config, "runtime", "account", "margin_usage", default=1.0),
        description="Account margin usage",
    )
    resolver = getattr(config, resolver_name, None)
    if not callable(resolver):
        return fallback
    try:
        resolved = resolver()
    except Exception:
        return fallback
    try:
        return _nonnegative_number(resolved, description=f"{resolver_name} result")
    except AccountingError:
        return fallback


@dataclass(frozen=True)
class AccountingPolicy:
    """Config-derived rules that determine how portfolio capital is counted."""

    wheel_margin_usage: float
    regime_margin_usage: float
    regime_weight_base: RegimeRebalanceBaseEnum
    portfolio_symbols: frozenset[str]
    regime_symbols: frozenset[str]
    cash_fund_symbol: str

    @classmethod
    def from_config(cls, config: Any) -> AccountingPolicy:
        raw_weight_base = _config_value(
            config,
            "strategies",
            "regime_rebalance",
            "weight_base",
            default=RegimeRebalanceBaseEnum.net_liq_ex_options,
        )
        try:
            weight_base = RegimeRebalanceBaseEnum(raw_weight_base)
        except (TypeError, ValueError) as exc:
            raise AccountingError("Regime weight base is invalid") from exc

        portfolio_symbols = _config_value(config, "portfolio", "symbols", default={})
        if not isinstance(portfolio_symbols, Mapping):
            portfolio_symbols = {}
        regime_symbols = _config_value(
            config, "strategies", "regime_rebalance", "symbols", default=[]
        )
        if not isinstance(regime_symbols, (list, tuple, set, frozenset)):
            regime_symbols = []
        cash_fund = str(
            _config_value(
                config,
                "strategies",
                "cash_management",
                "cash_fund",
                default="SGOV",
            )
        )
        return cls(
            wheel_margin_usage=_resolve_margin_usage(config, "wheel_margin_usage"),
            regime_margin_usage=_resolve_margin_usage(config, "regime_margin_usage"),
            regime_weight_base=weight_base,
            portfolio_symbols=frozenset(str(symbol) for symbol in portfolio_symbols),
            regime_symbols=frozenset(str(symbol) for symbol in regime_symbols),
            cash_fund_symbol=cash_fund,
        )


@dataclass(frozen=True)
class PositionLedger:
    """Portfolio market value split into explicit, non-overlapping buckets."""

    category_values: Mapping[PositionCategory, float]
    stock_quantities: Mapping[str, float]
    stock_values: Mapping[str, float]
    tail_values_by_con_id: Mapping[int, float]
    positions: tuple[PortfolioItem, ...] = field(repr=False)

    @classmethod
    def build(
        cls,
        portfolio_positions: PortfolioBySymbol | None,
        policy: AccountingPolicy,
        *,
        tail_owned_quantities: Mapping[int, int] | None = None,
    ) -> PositionLedger:
        owned = tail_owned_quantities or {}
        category_values = {category: 0.0 for category in PositionCategory}
        stock_quantities: dict[str, float] = {}
        stock_values: dict[str, float] = {}
        tail_values_by_con_id: dict[int, float] = {}
        positions = tuple(
            position
            for symbol_positions in (portfolio_positions or {}).values()
            for position in symbol_positions
        )

        for position in positions:
            contract = position.contract
            symbol = str(contract.symbol)
            market_value = _finite_number(
                getattr(position, "marketValue", 0.0) or 0.0,
                description=f"{symbol} position market value",
            )
            if isinstance(contract, Stock):
                quantity = _finite_number(
                    position.position,
                    description=f"{symbol} stock quantity",
                )
                if market_value <= 0 and quantity > 0:
                    market_price = _finite_number(
                        getattr(position, "marketPrice", 0.0) or 0.0,
                        description=f"{symbol} stock market price",
                    )
                    if market_price > 0:
                        market_value = quantity * market_price
                stock_quantities[symbol] = stock_quantities.get(symbol, 0.0) + quantity
                stock_values[symbol] = stock_values.get(symbol, 0.0) + market_value
                if symbol == policy.cash_fund_symbol:
                    category = PositionCategory.CASH_FUND
                elif symbol in policy.regime_symbols:
                    category = PositionCategory.REGIME_STOCK
                elif symbol in policy.portfolio_symbols:
                    category = PositionCategory.PORTFOLIO_STOCK
                else:
                    category = PositionCategory.OTHER_ASSET
                category_values[category] += market_value
                continue

            if not isinstance(contract, Option):
                category_values[PositionCategory.OTHER_ASSET] += market_value
                continue

            con_id = int(contract.conId) if type(contract.conId) is int else 0
            owned_quantity = int(owned.get(con_id, 0)) if con_id > 0 else 0
            live_quantity = _finite_number(
                position.position,
                description=f"{symbol} option quantity",
            )
            tail_value = 0.0
            if owned_quantity > 0 and live_quantity > 0:
                owned_value = owned_option_market_value(position, owned_quantity)
                if owned_value is not None:
                    tail_value = owned_value[1]
                    category_values[PositionCategory.TAIL_HEDGE_OPTION] += tail_value
                    tail_values_by_con_id[con_id] = tail_value

            remaining_value = market_value - tail_value
            option_category = (
                PositionCategory.REGIME_OPTION
                if symbol in policy.regime_symbols
                else PositionCategory.OTHER_OPTION
            )
            category_values[option_category] += remaining_value

        return cls(
            category_values=MappingProxyType(category_values),
            stock_quantities=MappingProxyType(stock_quantities),
            stock_values=MappingProxyType(stock_values),
            tail_values_by_con_id=MappingProxyType(tail_values_by_con_id),
            positions=positions,
        )

    def value(self, category: PositionCategory) -> float:
        return float(self.category_values.get(category, 0.0))

    @property
    def tail_hedge_value(self) -> float:
        return self.value(PositionCategory.TAIL_HEDGE_OPTION)

    @property
    def regime_option_value(self) -> float:
        return self.value(PositionCategory.REGIME_OPTION)

    def managed_stock_value(self, market_prices: Mapping[str, float]) -> float:
        value = 0.0
        for symbol, raw_price in market_prices.items():
            price = _finite_number(raw_price, description=f"{symbol} market price")
            if price <= 0:
                raise AccountingError(f"{symbol} market price must be positive")
            value += math.floor(self.stock_quantities.get(symbol, 0.0)) * price
        return value

    def stock_value(self, symbol: str, *, long_only: bool = False) -> float:
        if long_only and self.stock_quantities.get(symbol, 0.0) <= 0:
            return 0.0
        return float(self.stock_values.get(symbol, 0.0))


@dataclass(frozen=True)
class CapitalBase:
    """An auditable derivation of one strategy's usable capital."""

    kind: CapitalBaseKind
    value: float
    gross_value: float
    excluded_value: float
    margin_usage: float
    weight_base: RegimeRebalanceBaseEnum | None = None

    @property
    def adjusted_value(self) -> float:
        return self.gross_value - self.excluded_value


@dataclass(frozen=True)
class CashLedger:
    """Settled cash plus the reservations and pending flows applied to it."""

    settled_cash: float
    pending_debit: float = 0.0
    pending_credit: float = 0.0
    reserved_cash: float = 0.0
    ambiguous: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "settled_cash",
            _finite_number(self.settled_cash, description="Settled cash"),
        )
        object.__setattr__(
            self,
            "pending_debit",
            _nonnegative_number(self.pending_debit, description="Pending cash debit"),
        )
        object.__setattr__(
            self,
            "pending_credit",
            _nonnegative_number(
                self.pending_credit,
                description="Pending cash credit",
            ),
        )
        object.__setattr__(
            self,
            "reserved_cash",
            _nonnegative_number(self.reserved_cash, description="Reserved cash"),
        )

    @property
    def after_pending_debits(self) -> float:
        return self.settled_cash - self.pending_debit

    @property
    def sweepable_cash(self) -> float:
        return self.after_pending_debits - self.reserved_cash

    @property
    def projected_cash(self) -> float:
        return self.after_pending_debits + self.pending_credit

    def amount_to_sweep(
        self,
        *,
        target_cash: float,
        buy_threshold: float,
        sell_threshold: float,
    ) -> float:
        if self.ambiguous:
            raise AccountingError("Pending order cash cannot be priced safely")
        target = _nonnegative_number(target_cash, description="Target cash")
        buy_band = _nonnegative_number(buy_threshold, description="Cash buy threshold")
        sell_band = _nonnegative_number(
            sell_threshold,
            description="Cash sell threshold",
        )
        if self.sweepable_cash > target + buy_band:
            return self.sweepable_cash - target
        # Credits may prevent a duplicate liquidation, but cannot fund a buy.
        if self.projected_cash < target - sell_band:
            return self.projected_cash - target
        return 0.0


@dataclass(frozen=True)
class PortfolioAccounting:
    """Shared entry point for every configured portfolio accounting view."""

    account: BrokerAccountSnapshot
    policy: AccountingPolicy
    positions: PositionLedger

    @classmethod
    def build(
        cls,
        *,
        config: Any,
        account_summary: AccountSummary,
        portfolio_positions: PortfolioBySymbol | None = None,
        tail_owned_quantities: Mapping[int, int] | None = None,
        regime_symbols: Iterable[str] | None = None,
    ) -> PortfolioAccounting:
        policy = AccountingPolicy.from_config(config)
        if regime_symbols is not None:
            policy = replace(
                policy,
                regime_symbols=frozenset(str(symbol) for symbol in regime_symbols),
            )
        return cls(
            account=BrokerAccountSnapshot(account_summary),
            policy=policy,
            positions=PositionLedger.build(
                portfolio_positions,
                policy,
                tail_owned_quantities=tail_owned_quantities,
            ),
        )

    @classmethod
    def from_net_liquidation(
        cls,
        *,
        config: Any,
        net_liquidation: float,
        portfolio_positions: PortfolioBySymbol | None = None,
        tail_owned_quantities: Mapping[int, int] | None = None,
        regime_symbols: Iterable[str] | None = None,
    ) -> PortfolioAccounting:
        policy = AccountingPolicy.from_config(config)
        if regime_symbols is not None:
            policy = replace(
                policy,
                regime_symbols=frozenset(str(symbol) for symbol in regime_symbols),
            )
        return cls(
            account=BrokerAccountSnapshot.from_balances(
                net_liquidation=net_liquidation
            ),
            policy=policy,
            positions=PositionLedger.build(
                portfolio_positions,
                policy,
                tail_owned_quantities=tail_owned_quantities,
            ),
        )

    def capital_base(
        self,
        kind: CapitalBaseKind,
        *,
        market_prices: Mapping[str, float] | None = None,
        tail_hedge_value_override: float | None = None,
    ) -> CapitalBase:
        net_liquidation = self.account.net_liquidation
        if kind == CapitalBaseKind.NET_LIQUIDATION:
            return CapitalBase(kind, net_liquidation, net_liquidation, 0, 1)
        if kind == CapitalBaseKind.WHEEL_BUYING_POWER:
            margin_usage = self.policy.wheel_margin_usage
            return CapitalBase(
                kind,
                math.floor(net_liquidation * margin_usage),
                net_liquidation,
                0,
                margin_usage,
            )
        if kind != CapitalBaseKind.REGIME_REBALANCE:
            raise AccountingError(f"Unsupported capital base: {kind}")

        weight_base = self.policy.regime_weight_base
        if weight_base == RegimeRebalanceBaseEnum.managed_stocks:
            if market_prices is None:
                raise AccountingError("Managed-stock accounting requires market prices")
            value = self.positions.managed_stock_value(market_prices)
            return CapitalBase(kind, value, value, 0, 1, weight_base)

        reported_tail_value = self.positions.tail_hedge_value
        tail_value = (
            reported_tail_value
            if tail_hedge_value_override is None
            else _finite_number(
                tail_hedge_value_override,
                description="Tail-hedge value override",
            )
        )
        excluded_value = tail_value
        if weight_base == RegimeRebalanceBaseEnum.net_liq_ex_options:
            excluded_value += self.positions.regime_option_value
        margin_usage = self.policy.regime_margin_usage
        return CapitalBase(
            kind,
            math.floor((net_liquidation - excluded_value) * margin_usage),
            net_liquidation,
            excluded_value,
            margin_usage,
            weight_base,
        )

    def cash_ledger(
        self,
        *,
        pending_debit: float = 0.0,
        pending_credit: float = 0.0,
        reserved_cash: float = 0.0,
        ambiguous: bool = False,
    ) -> CashLedger:
        return CashLedger(
            settled_cash=math.floor(self.account.total_cash),
            pending_debit=pending_debit,
            pending_credit=pending_credit,
            reserved_cash=reserved_cash,
            ambiguous=ambiguous,
        )


def order_cash_notional(
    contract: Contract,
    order: Any,
    qualified_contracts: Mapping[int, Contract] | None = None,
) -> float:
    """Return limit-price notional in contract currency."""
    order_type = str(getattr(order, "orderType", "") or "").upper()
    if order_type and order_type != "LMT":
        raise ValueError("Order cash notional requires a limit order")
    multiplier = 1.0
    if contract.secType != "STK":
        raw_multiplier: Any = contract.multiplier
        if contract.secType == "BAG" and qualified_contracts and contract.comboLegs:
            leg = qualified_contracts.get(contract.comboLegs[0].conId)
            if leg is not None:
                raw_multiplier = leg.multiplier
        multiplier = float(raw_multiplier or 100)

    price = float(getattr(order, "lmtPrice", 0) or 0)
    quantity = float(getattr(order, "totalQuantity", 0) or 0)
    if (
        not math.isfinite(price)
        or math.isclose(price, 0.0, abs_tol=1e-12)
        or not math.isfinite(quantity)
        or quantity <= 0
        or not math.isfinite(multiplier)
        or multiplier <= 0
    ):
        raise ValueError(
            "Order cash notional requires finite price, size, and multiplier"
        )
    notional = price * quantity * multiplier
    if not math.isfinite(notional):
        raise ValueError("Order cash notional must be finite")
    return notional


class PendingBuyCash(NamedTuple):
    debit: float
    ambiguous: bool


class PendingOrderCash(NamedTuple):
    debit: float
    credit: float
    ambiguous: bool

    @property
    def net_change(self) -> float:
        return self.credit - self.debit


def working_order_cash(
    trades: Iterable[Any],
    *,
    account: str,
    qualified_contracts: Mapping[int, Contract] | None = None,
    estimated_fee_per_contract: float = 0.0,
) -> PendingOrderCash:
    """Return remaining debits and credits for active-account working orders."""
    debit = 0.0
    credit = 0.0
    ambiguous = False
    for trade in trades:
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        is_done = getattr(trade, "isDone", None)
        action = str(getattr(order, "action", "")).upper()
        if (
            order is None
            or contract is None
            or (callable(is_done) and is_done())
            or getattr(order, "account", None) != account
            or action not in {"BUY", "SELL"}
        ):
            continue
        try:
            total_quantity = float(getattr(order, "totalQuantity", 0) or 0)
        except (TypeError, ValueError):
            ambiguous = True
            continue
        if not math.isfinite(total_quantity) or total_quantity <= 0:
            ambiguous = True
            continue
        try:
            remaining = float(
                getattr(getattr(trade, "orderStatus", None), "remaining", 0)
            )
        except (TypeError, ValueError):
            remaining = 0.0
        if not math.isfinite(remaining) or remaining <= 0 or remaining > total_quantity:
            ambiguous = True
            remaining = total_quantity
        try:
            notional = order_cash_notional(contract, order, qualified_contracts)
            fee = (
                0.0
                if contract.secType == "STK"
                else float(estimated_fee_per_contract) * remaining
            )
            if not math.isfinite(fee) or fee < 0:
                raise ValueError("Estimated order fee must be finite and non-negative")
            cash_change = (notional if action == "SELL" else -notional) * (
                remaining / total_quantity
            ) - fee
        except (TypeError, ValueError, OverflowError):
            ambiguous = True
            continue
        if not math.isfinite(cash_change):
            ambiguous = True
            continue
        debit += max(0.0, -cash_change)
        credit += max(0.0, cash_change)
    return PendingOrderCash(debit, credit, ambiguous)


def queued_order_cash(
    records: Iterable[tuple[Contract, Any, Any]],
    qualified_contracts: Mapping[int, Contract] | None = None,
    estimated_fee_per_contract: float = 0.0,
) -> PendingOrderCash:
    """Return debits and credits for locally queued orders."""
    debit = 0.0
    credit = 0.0
    ambiguous = False
    for contract, order, _intent_id in records:
        action = str(getattr(order, "action", "")).upper()
        if action not in {"BUY", "SELL"}:
            continue
        try:
            notional = order_cash_notional(contract, order, qualified_contracts)
            quantity = float(getattr(order, "totalQuantity", 0) or 0)
            fee = (
                0.0
                if contract.secType == "STK"
                else float(estimated_fee_per_contract) * quantity
            )
            if not math.isfinite(fee) or fee < 0:
                raise ValueError("Estimated order fee must be finite and non-negative")
            cash_change = (notional if action == "SELL" else -notional) - fee
        except (TypeError, ValueError, OverflowError):
            ambiguous = True
            continue
        if not math.isfinite(cash_change):
            ambiguous = True
            continue
        debit += max(0.0, -cash_change)
        credit += max(0.0, cash_change)
    return PendingOrderCash(debit, credit, ambiguous)


def pending_order_cash(
    trades: Iterable[Any],
    records: Iterable[tuple[Contract, Any, Any]],
    *,
    account: str,
    qualified_contracts: Mapping[int, Contract] | None = None,
    estimated_fee_per_contract: float = 0.0,
) -> PendingOrderCash:
    """Combine broker-working and locally queued order cash flows."""
    working = working_order_cash(
        trades,
        account=account,
        qualified_contracts=qualified_contracts,
        estimated_fee_per_contract=estimated_fee_per_contract,
    )
    queued = queued_order_cash(
        records,
        qualified_contracts,
        estimated_fee_per_contract,
    )
    debit = working.debit + queued.debit
    credit = working.credit + queued.credit
    return PendingOrderCash(
        debit if math.isfinite(debit) else 0.0,
        credit if math.isfinite(credit) else 0.0,
        working.ambiguous
        or queued.ambiguous
        or not math.isfinite(debit)
        or not math.isfinite(credit),
    )


def working_buy_cash(
    trades: Iterable[Any],
    *,
    account: str,
    qualified_contracts: Mapping[int, Contract] | None = None,
    estimated_fee_per_contract: float = 0.0,
) -> PendingBuyCash:
    """Return unfilled active-account BUY debit and snapshot ambiguity."""
    pending = working_order_cash(
        (
            trade
            for trade in trades
            if str(getattr(getattr(trade, "order", None), "action", "")).upper()
            == "BUY"
        ),
        account=account,
        qualified_contracts=qualified_contracts,
        estimated_fee_per_contract=estimated_fee_per_contract,
    )
    return PendingBuyCash(pending.debit, pending.ambiguous)


def queued_buy_cash(
    records: Iterable[tuple[Contract, Any, Any]],
    qualified_contracts: Mapping[int, Contract] | None = None,
    estimated_fee_per_contract: float = 0.0,
) -> PendingBuyCash:
    """Return queued BUY debit, marking orders without a usable limit ambiguous."""
    pending = queued_order_cash(
        (
            record
            for record in records
            if str(getattr(record[1], "action", "")).upper() == "BUY"
        ),
        qualified_contracts,
        estimated_fee_per_contract,
    )
    return PendingBuyCash(pending.debit, pending.ambiguous)


def pending_buy_cash(
    trades: Iterable[Any],
    records: Iterable[tuple[Contract, Any, Any]],
    *,
    account: str,
    qualified_contracts: Mapping[int, Contract] | None = None,
    estimated_fee_per_contract: float = 0.0,
) -> PendingBuyCash:
    """Combine broker-working and locally queued BUY reservations."""
    working = working_buy_cash(
        trades,
        account=account,
        qualified_contracts=qualified_contracts,
        estimated_fee_per_contract=estimated_fee_per_contract,
    )
    queued = queued_buy_cash(
        records,
        qualified_contracts,
        estimated_fee_per_contract,
    )
    debit = working.debit + queued.debit
    return PendingBuyCash(
        debit if math.isfinite(debit) else 0.0,
        working.ambiguous or queued.ambiguous or not math.isfinite(debit),
    )
