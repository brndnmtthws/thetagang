import math
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

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


class Orders:
    def __init__(self) -> None:
        self.__records: list[tuple[Contract, LimitOrder, int | None]] = []

    def add_order(
        self, contract: Contract, order: LimitOrder, intent_id: int | None
    ) -> None:
        self.__records.append((contract, order, intent_id))

    def records(self) -> list[tuple[Contract, LimitOrder, int | None]]:
        return self.__records

    def remove_records(
        self,
        records: Iterable[tuple[Contract, LimitOrder, int | None]],
    ) -> None:
        record_ids = {id(record) for record in records}
        self.__records[:] = [
            record for record in self.__records if id(record) not in record_ids
        ]

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
