from ib_async import Contract, Order, Trade
from rich import box
from rich.pretty import Pretty
from rich.table import Table

from thetagang import log
from thetagang.db import DataStore
from thetagang.fmt import dfmt, ffmt, ifmt
from thetagang.ibkr import IBKR


class Trades:
    def __init__(self, ibkr: IBKR, data_store: DataStore | None = None) -> None:
        self.ibkr = ibkr
        self.data_store = data_store
        self.__records: list[Trade] = []

    def submit_order(
        self,
        contract: Contract,
        order: Order,
        idx: int | None = None,
        intent_id: int | None = None,
    ) -> bool:
        """Submit an order, returning whether local placement succeeded."""
        try:
            trade = self.ibkr.place_order(contract, order)
        except Exception as exc:  # noqa: BLE001
            log.error(
                f"{contract.symbol}: Failed to submit contract, order={order}, "
                f"error={type(exc).__name__}: {exc}"
            )
            return False

        if self.data_store:
            self.data_store.record_order(contract, order, intent_id=intent_id)
        if idx is not None:
            self.__replace_trade(trade, idx)
        else:
            self.__add_trade(trade)
        return True

    def records(self) -> list[Trade]:
        return self.__records

    def is_empty(self) -> bool:
        return len(self.__records) == 0

    def print_summary(self) -> None:
        if not self.__records:
            return

        table = Table(
            title="Trade Summary", show_lines=True, box=box.MINIMAL_HEAVY_HEAD
        )
        table.add_column("Symbol")
        table.add_column("Exchange")
        table.add_column("Contract")
        table.add_column("Action")
        table.add_column("Price")
        table.add_column("Qty")
        table.add_column("Status")
        table.add_column("Filled")

        for trade in self.__records:
            table.add_row(
                trade.contract.symbol,
                trade.contract.exchange,
                Pretty(trade.contract, indent_size=2),
                trade.order.action,
                dfmt(
                    float(trade.order.lmtPrice)
                    if trade.order.lmtPrice is not None
                    else None
                ),
                ifmt(int(trade.order.totalQuantity)),
                trade.orderStatus.status,
                ffmt(trade.orderStatus.filled, 0),
            )

        log.print(table)

    def __add_trade(self, trade: Trade) -> None:
        self.__records.append(trade)

    def __replace_trade(self, trade: Trade, idx: int) -> None:
        self.__records[idx] = trade
