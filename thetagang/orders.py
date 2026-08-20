from collections.abc import Iterable
from typing import List, Optional, Tuple

from ib_async import Contract, LimitOrder
from rich import box
from rich.pretty import Pretty
from rich.table import Table

from thetagang import log
from thetagang.accounting import (
    PendingBuyCash,
    PendingOrderCash,
    order_cash_notional,
    pending_buy_cash,
    pending_order_cash,
    queued_buy_cash,
    queued_order_cash,
    working_buy_cash,
    working_order_cash,
)
from thetagang.fmt import dfmt, ifmt

__all__ = [
    "Orders",
    "PendingBuyCash",
    "PendingOrderCash",
    "order_cash_notional",
    "pending_buy_cash",
    "pending_order_cash",
    "queued_buy_cash",
    "queued_order_cash",
    "working_buy_cash",
    "working_order_cash",
]


class Orders:
    def __init__(self) -> None:
        self.__records: List[Tuple[Contract, LimitOrder, Optional[int]]] = []

    def add_order(
        self, contract: Contract, order: LimitOrder, intent_id: Optional[int]
    ) -> None:
        self.__records.append((contract, order, intent_id))

    def records(self) -> List[Tuple[Contract, LimitOrder, Optional[int]]]:
        return self.__records

    def remove_records(
        self,
        records: Iterable[Tuple[Contract, LimitOrder, Optional[int]]],
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
