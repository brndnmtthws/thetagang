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
    return price * quantity * multiplier


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
        total_quantity = float(getattr(order, "totalQuantity", 0) or 0)
        try:
            remaining = float(
                getattr(getattr(trade, "orderStatus", None), "remaining", 0)
            )
        except (TypeError, ValueError):
            remaining = 0.0
        if not math.isfinite(remaining) or remaining <= 0 or remaining > total_quantity:
            ambiguous = True
            remaining = total_quantity
        debit += max(
            0.0,
            order_cash_notional(contract, order, qualified_contracts)
            * (remaining / total_quantity if total_quantity > 0 else 0.0),
        )
    return PendingBuyCash(debit, ambiguous)


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
