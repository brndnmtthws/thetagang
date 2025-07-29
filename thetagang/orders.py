from typing import List, Tuple

from ib_async import Contract, LimitOrder
from rich import box
from rich.pretty import Pretty
from rich.table import Table

from thetagang import log
from thetagang.fmt import dfmt, ifmt


class Orders:
    def __init__(self) -> None:
        self.__records: List[Tuple[Contract, LimitOrder]] = []

    def add_order(self, contract: Contract, order: LimitOrder) -> None:
        self.__records.append((contract, order))

    def records(self) -> List[Tuple[Contract, LimitOrder]]:
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

        for contract, order in self.__records:
            table.add_row(
                contract.symbol,
                contract.exchange,
                Pretty(contract, indent_size=2),
                order.action,
                dfmt(float(order.lmtPrice) if order.lmtPrice is not None else None),
                ifmt(int(order.totalQuantity)),
            )

        log.print(table)
