"""Shared order exposure and conservative checks before any batch is submitted."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from ib_async import AccountValue, Contract, Order, OrderState, PortfolioItem, Trade

from thetagang.execution import ExecutionError


class BatchRiskError(ExecutionError):
    """The complete batch must be replanned before any order may be submitted."""


def finite(value: float | str | Decimal | None, description: str) -> float:
    try:
        if value is None:
            raise ValueError("Missing value")
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise BatchRiskError(f"Missing or invalid {description}") from exc
    if not math.isfinite(number) or abs(number) >= 1e100:
        raise BatchRiskError(f"Missing or invalid {description}")
    return number


def contract_key(contract: Contract) -> tuple[str, ...]:
    return (
        contract.secType,
        contract.symbol,
        contract.currency,
        contract.lastTradeDateOrContractMonth,
        str(contract.strike),
        contract.right,
        contract.multiplier if contract.secType != "STK" else "1",
    )


def multiplier(contract: Contract, qualified: Mapping[int, Contract]) -> float:
    if contract.secType == "BAG":
        if not contract.comboLegs:
            raise BatchRiskError("Combo has no legs")
        values = set()
        for leg in contract.comboLegs:
            component = qualified.get(leg.conId)
            if component is None:
                raise BatchRiskError(f"Unqualified combo leg {leg.conId}")
            values.add(multiplier(component, qualified))
        if len(values) != 1:
            raise BatchRiskError("Mixed combo multipliers are unsupported")
        return values.pop()
    if contract.secType == "STK":
        return 1.0
    if contract.secType != "OPT":
        raise BatchRiskError(f"Unsupported risk contract type: {contract.secType}")
    value = finite(contract.multiplier, "option multiplier")
    if value <= 0:
        raise BatchRiskError("Option multiplier must be positive")
    return value


def order_legs(
    contract: Contract, order: Order, qualified: Mapping[int, Contract]
) -> list[tuple[Contract, float]]:
    if order.action not in {"BUY", "SELL"}:
        raise BatchRiskError(f"Unsupported order action: {order.action}")
    quantity = finite(order.totalQuantity, "quantity")
    if quantity <= 0:
        raise BatchRiskError("Quantity must be positive")
    signed = quantity if order.action == "BUY" else -quantity
    if contract.secType != "BAG":
        multiplier(contract, qualified)
        return [(contract, signed)]
    multiplier(contract, qualified)
    legs = []
    for leg in contract.comboLegs:
        if leg.action not in {"BUY", "SELL"} or leg.ratio <= 0:
            raise BatchRiskError("Invalid combo leg action or ratio")
        component = qualified[leg.conId]
        if component.currency != contract.currency:
            raise BatchRiskError("Mixed combo currencies are unsupported")
        legs.append(
            (component, signed * leg.ratio * (1 if leg.action == "BUY" else -1))
        )
    return legs


def pending_debit(
    contract: Contract, order: Order, qualified: Mapping[int, Contract]
) -> float:
    order_legs(contract, order, qualified)
    price = finite(order.lmtPrice, "limit price")
    if order.orderType != "LMT" or (contract.secType != "BAG" and price <= 0):
        raise BatchRiskError("Risk checks require a valid limit order")
    signed_price = price if order.action == "BUY" else -price
    return max(
        0.0, signed_price * float(order.totalQuantity) * multiplier(contract, qualified)
    )


def remaining_orders(
    trades: Sequence[Trade], account: str
) -> list[tuple[Contract, Order]]:
    from copy import copy

    result = []
    for trade in trades:
        if trade.order.account != account or trade.isDone():
            continue
        if trade.orderStatus.status not in {"Submitted", "PreSubmitted"}:
            raise BatchRiskError("An existing order has an unresolved broker status")
        remaining = max(
            finite(trade.orderStatus.remaining, "remaining quantity"),
            finite(trade.order.totalQuantity, "total quantity")
            - finite(trade.orderStatus.filled, "filled quantity"),
        )
        if remaining > 0:
            order = copy(trade.order)
            order.totalQuantity = remaining
            result.append((trade.contract, order))
    return result


def projected_positions(
    positions: Mapping[str, Sequence[PortfolioItem]],
    orders: Sequence[tuple[Contract, Order]],
    qualified: Mapping[int, Contract],
    *,
    include_new: bool = True,
) -> dict[str, list[PortfolioItem]]:
    items = {contract_key(p.contract): p for group in positions.values() for p in group}
    actual_keys = set(items)
    for contract, order in orders:
        for component, quantity in order_legs(contract, order, qualified):
            key = contract_key(component)
            previous = items.get(key)
            if previous is None:
                if not include_new:
                    continue
                price = (
                    abs(finite(order.lmtPrice, "limit price"))
                    if contract.secType != "BAG"
                    else 0.0
                )
                previous = PortfolioItem(component, 0, price, 0, 0, 0, 0, order.account)
            new_quantity = previous.position + quantity
            if not include_new:
                # Management stages can close actual holdings only. New writes
                # must not become synthetic positions eligible for a second roll.
                if key not in actual_keys or previous.position * quantity >= 0:
                    continue
                new_quantity = (
                    max(0.0, new_quantity)
                    if previous.position > 0
                    else min(0.0, new_quantity)
                )
            items[key] = previous._replace(
                position=new_quantity,
                marketValue=new_quantity
                * previous.marketPrice
                * multiplier(component, qualified),
            )
    result: dict[str, list[PortfolioItem]] = defaultdict(list)
    for item in items.values():
        if item.position:
            result[item.contract.symbol].append(item)
    return dict(result)


@dataclass(frozen=True)
class BatchReservation:
    debit: float
    available_funds: float
    excess_liquidity: float
    currency: str


def margin_cost(
    state: OrderState, currency: str, fee_buffer: float
) -> tuple[float, float]:
    if state.warningText or state.status in {"Inactive", "Cancelled", "ApiCancelled"}:
        raise BatchRiskError(
            f"Broker margin preview was not clean: {state.warningText or state.status}"
        )
    equity_debit = max(0.0, -finite(state.equityWithLoanChange, "equity change"))
    fee = fee_buffer
    for estimate in (state.commission, state.maxCommission):
        if math.isfinite(estimate) and 0 <= estimate < 1e100:
            if state.commissionCurrency != currency:
                raise BatchRiskError(
                    "Commission currency differs from account currency"
                )
            fee = max(fee, estimate)
    return (
        max(0.0, finite(state.initMarginChange, "initial margin change"))
        + equity_debit
        + fee,
        max(0.0, finite(state.maintMarginChange, "maintenance margin change"))
        + equity_debit
        + fee,
    )


def validate_batch(
    *,
    account: str,
    summary: Mapping[str, AccountValue],
    positions: Sequence[PortfolioItem],
    proposed: Sequence[tuple[Contract, Order]],
    working: Sequence[Trade],
    qualified: Mapping[int, Contract],
    funds_buffer: float = 0.0,
) -> BatchReservation:
    def account_value(tag: str) -> AccountValue:
        value = summary.get(tag)
        if value is None or value.account != account:
            raise BatchRiskError(f"Missing {tag} for the selected account")
        return value

    available = account_value("AvailableFunds")
    currency = available.currency
    if not currency or currency == "BASE":
        raise BatchRiskError(
            "An explicit account base currency is required for risk checks"
        )
    funds = finite(available.value, "available funds") - funds_buffer
    excess = account_value("ExcessLiquidity")
    buying_power = account_value("BuyingPower")
    if excess.currency != currency or buying_power.currency != currency:
        raise BatchRiskError("Account risk values have inconsistent currencies")
    liquidity = finite(excess.value, "excess liquidity") - funds_buffer
    power = finite(buying_power.value, "buying power") - funds_buffer
    existing = remaining_orders(working, account)
    # Working orders already affect broker margin, but reserve their remaining
    # debit again for the gross purchasing cap: conservative, never a credit.
    debit = 0.0
    seen: set[tuple[str, ...]] = set()
    holdings = {
        contract_key(p.contract): float(p.position)
        for p in positions
        if p.account == account
    }
    shares: dict[str, float] = defaultdict(float)
    calls: dict[str, float] = defaultdict(float)
    for position in positions:
        if position.account != account:
            continue
        c = position.contract
        if c.secType == "STK":
            shares[c.symbol] += position.position
        elif c.secType == "OPT" and c.right == "C" and position.position < 0:
            calls[c.symbol] += -position.position * multiplier(c, qualified)
    affected: set[str] = set()
    for index, (contract, order) in enumerate([*existing, *proposed]):
        if order.account != account or contract.currency != currency:
            raise BatchRiskError(
                "Order account/currency does not match the risk snapshot"
            )
        debit += pending_debit(contract, order, qualified)
        legs = order_legs(contract, order, qualified)
        keys = {contract_key(c) for c, _ in legs}
        if index >= len(existing) and keys & seen:
            raise BatchRiskError(
                "Duplicate or opposing orders touch the same instrument"
            )
        seen.update(keys)
        call_increase: dict[str, float] = defaultdict(float)
        for component, quantity in legs:
            key = contract_key(component)
            if component.secType == "STK" and quantity < 0:
                shares[component.symbol] += quantity
                affected.add(component.symbol)
                if shares[component.symbol] < -1e-8:
                    raise BatchRiskError(
                        "Stock sales exceed actual unreserved holdings"
                    )
            elif component.secType == "OPT" and component.right == "C":
                before = holdings.get(key, 0.0)
                after = before + quantity
                call_increase[component.symbol] += (
                    max(0.0, -after) - max(0.0, -before)
                ) * multiplier(component, qualified)
                if quantity < 0:
                    affected.add(component.symbol)
        # A combo can move coverage between its legs. Separate pending closes
        # never release cover for another order before they actually fill.
        for symbol, increase in call_increase.items():
            calls[symbol] += max(0.0, increase)
    if any(calls[symbol] > max(0.0, shares[symbol]) + 1e-8 for symbol in affected):
        raise BatchRiskError(
            "Call coverage would rely on unfilled orders or sold shares"
        )
    if debit > max(0.0, power) + 1e-8:
        raise BatchRiskError("Combined pending debits exceed available buying power")
    return BatchReservation(debit, funds, liquidity, currency)
