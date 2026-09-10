from typing import List, Optional

from ib_async import Contract, Order, Trade
from rich import box
from rich.pretty import Pretty
from rich.table import Table

from thetagang import log
from thetagang.db import DataStore
from thetagang.execution import ExecutionError, OrderSubmissionError
from thetagang.execution_state import ExecutionJournal, unique_order_ref
from thetagang.fmt import dfmt, ffmt, ifmt
from thetagang.ibkr import IBKR, DryRunOrderError


class Trades:
    def __init__(
        self,
        ibkr: IBKR,
        data_store: Optional[DataStore] = None,
        journal: ExecutionJournal | None = None,
    ) -> None:
        self.ibkr = ibkr
        self.data_store = data_store
        self.__records: List[Trade] = []
        self.__intent_ids: List[Optional[int]] = []
        self.journal = journal
        self._attempted_refs: set[str] = set()

    def submit_order(
        self,
        contract: Contract,
        order: Order,
        idx: Optional[int] = None,
        intent_id: Optional[int] = None,
    ) -> Trade:
        self.ibkr.ensure_order_execution_allowed()
        if idx is not None:
            # Resolve and validate the replacement before touching the broker.
            previous_intent_id = self.__intent_ids[idx]
            if intent_id is None:
                intent_id = previous_intent_id
            if self.journal:
                self.journal.observe(self.__records[idx])
        elif not order.orderRef:
            order.orderRef = unique_order_ref("tg:order")
        if idx is None and order.orderRef in self._attempted_refs:
            raise ExecutionError("Order reference has already been attempted")
        if self.journal:
            self.journal.begin(contract, order, amendment=idx is not None)
        self._attempted_refs.add(order.orderRef)
        try:
            trade = self.ibkr.place_order(contract, order)
        except DryRunOrderError:
            raise
        except Exception as exc:
            if self.data_store:
                self.data_store.record_event(
                    "order_submission_unknown",
                    {"intent_id": intent_id, "order_ref": order.orderRef},
                    symbol=contract.symbol,
                )
            raise OrderSubmissionError(
                f"{contract.symbol}: order submission failed; reconcile with the broker "
                "before retrying."
            ) from exc
        if idx is not None:
            self.__replace_trade(trade, idx)
            self.__intent_ids[idx] = intent_id
        else:
            self.__add_trade(trade)
            self.__intent_ids.append(intent_id)
        try:
            if self.journal:
                self.journal.observe(trade)
            if self.data_store:
                self.data_store.record_order(contract, order, intent_id=intent_id)
        except Exception as exc:
            raise OrderSubmissionError(
                "Broker accepted the request but recording its result failed; reconcile before retrying."
            ) from exc
        return trade

    def records(self) -> List[Trade]:
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
