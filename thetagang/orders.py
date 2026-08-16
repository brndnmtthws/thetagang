import math
from collections.abc import Iterable, Mapping
from typing import Any, List, NamedTuple, Optional, Tuple

from ib_async import Contract, LimitOrder
from rich import box
from rich.pretty import Pretty
from rich.table import Table

from thetagang import log
from thetagang.fmt import dfmt, ifmt


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
        or not math.isfinite(quantity)
        or quantity < 0
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


def working_buy_cash(
    trades: Iterable[Any],
    *,
    account: str,
    qualified_contracts: Mapping[int, Contract] | None = None,
) -> PendingBuyCash:
    """Return unfilled active-account BUY debit and snapshot ambiguity."""
    debit = 0.0
    ambiguous = False
    for trade in trades:
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        is_done = getattr(trade, "isDone", None)
        if (
            order is None
            or contract is None
            or (callable(is_done) and is_done())
            or getattr(order, "account", None) != account
            or str(getattr(order, "action", "")).upper() != "BUY"
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
            amount = max(
                0.0,
                order_cash_notional(contract, order, qualified_contracts)
                * (remaining / total_quantity),
            )
        except (TypeError, ValueError, OverflowError):
            ambiguous = True
            continue
        if not math.isfinite(debit + amount):
            ambiguous = True
            continue
        debit += amount
    return PendingBuyCash(debit, ambiguous)


def queued_buy_cash(
    records: Iterable[tuple[Contract, Any, Any]],
    qualified_contracts: Mapping[int, Contract] | None = None,
) -> PendingBuyCash:
    """Return queued BUY debit, marking orders without a usable limit ambiguous."""
    debit = 0.0
    ambiguous = False
    for contract, order, _intent_id in records:
        if str(getattr(order, "action", "")).upper() != "BUY":
            continue
        try:
            amount = max(
                0.0,
                order_cash_notional(contract, order, qualified_contracts),
            )
        except (TypeError, ValueError, OverflowError):
            ambiguous = True
            continue
        if not math.isfinite(debit + amount):
            ambiguous = True
            continue
        debit += amount
    return PendingBuyCash(debit, ambiguous)


def pending_buy_cash(
    trades: Iterable[Any],
    records: Iterable[tuple[Contract, Any, Any]],
    *,
    account: str,
    qualified_contracts: Mapping[int, Contract] | None = None,
) -> PendingBuyCash:
    """Combine broker-working and locally queued BUY reservations."""
    working = working_buy_cash(
        trades,
        account=account,
        qualified_contracts=qualified_contracts,
    )
    queued = queued_buy_cash(records, qualified_contracts)
    debit = working.debit + queued.debit
    return PendingBuyCash(
        debit if math.isfinite(debit) else 0.0,
        working.ambiguous or queued.ambiguous or not math.isfinite(debit),
    )


class Orders:
    def __init__(self) -> None:
        self.__records: List[Tuple[Contract, LimitOrder, Optional[int]]] = []

    def add_order(
        self, contract: Contract, order: LimitOrder, intent_id: Optional[int]
    ) -> None:
        self.__records.append((contract, order, intent_id))

    def records(self) -> List[Tuple[Contract, LimitOrder, Optional[int]]]:
        return self.__records

    def print_summary(self) -> None:
        if not self.__records:
            return

        table = Table(
            title="Order Summary", show_lines=True, box=box.MINIMAL_HEAVY_HEAD
        )
        table.add_column("Symbol")
        table.add_column("Exchange")
        table.add_column("Contract")
        table.add_column("Action")
        table.add_column("Price")
        table.add_column("Qty")

        for contract, order, _intent_id in self.__records:
            table.add_row(
                contract.symbol,
                contract.exchange,
                Pretty(contract, indent_size=2),
                order.action,
                dfmt(float(order.lmtPrice) if order.lmtPrice is not None else None),
                ifmt(int(order.totalQuantity)),
            )

        log.print(table)
