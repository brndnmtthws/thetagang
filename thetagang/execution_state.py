"""Local account mutex and durable, fail-closed order submission journal."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterator, Literal, Sequence
from uuid import uuid4

from ib_async import Contract, Execution, Fill, Order, Trade
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from thetagang.execution import ExecutionError


def account_state_path(directory: str, account: str) -> Path:
    # Do not expose account numbers in filenames; all clients for an account
    # must use the same directory, regardless of configuration file location.
    key = hashlib.sha256(account.encode()).hexdigest()[:24]
    return Path(directory).expanduser().resolve() / key


@contextmanager
def account_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a+b") as handle:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.write(b"\0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ExecutionError(
                "Another ThetaGang run holds this account's execution lock."
            ) from exc
        # Closing the handle releases the OS lock even after an exception.
        # Never unlink the lock file: that would allow two locked inodes.
        yield


def unique_order_ref(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    quantity: float = Field(gt=0, allow_inf_nan=False)
    symbol: str
    sec_type: str
    con_id: int
    state: Literal["submitting", "acknowledged", "unknown", "terminal"]
    order_id: int = 0
    client_id: int = 0
    perm_id: int = 0
    status: str = ""


class JournalData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    account: str
    submissions: dict[str, Submission] = Field(default_factory=dict)


class ExecutionJournal:
    """Persist before submitting; missing broker evidence never implies failure.

    The caller must hold account_lock for the journal's entire lifetime.
    Completed entries are retained to prevent reuse of an old submission ref.
    """

    def __init__(self, path: Path, account: str) -> None:
        self.path = path.with_suffix(".json")
        try:
            self.data = (
                JournalData.model_validate_json(self.path.read_text())
                if self.path.exists()
                else JournalData(account=account)
            )
            if self.data.account != account or any(
                key != entry.ref for key, entry in self.data.submissions.items()
            ):
                raise ValueError("Journal account or reference mismatch")
        except (OSError, ValueError, ValidationError) as exc:
            raise ExecutionError(
                f"Cannot trust execution journal: {self.path}"
            ) from exc

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp_path: Path | None = None
            try:
                with NamedTemporaryFile(
                    mode="w", dir=self.path.parent, delete=False, encoding="utf8"
                ) as handle:
                    temp_path = Path(handle.name)
                    json.dump(self.data.model_dump(), handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.path)
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
            if os.name != "nt":
                descriptor = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as exc:
            raise ExecutionError(
                f"Execution journal could not be persisted: {self.path}. "
                "Stop and reconcile broker orders before retrying."
            ) from exc

    def begin(self, contract: Contract, order: Order, *, amendment: bool) -> None:
        if order.account != self.data.account:
            raise ExecutionError("Order account does not match execution journal")
        previous = self.data.submissions.get(order.orderRef)
        if previous and not amendment:
            raise ExecutionError("Order reference has already been submitted")
        if amendment and (
            previous is None
            or previous.state != "acknowledged"
            or previous.order_id != order.orderId
            or previous.client_id != order.clientId
        ):
            raise ExecutionError("Cannot amend an unconfirmed or different order")
        self.data.submissions[order.orderRef] = Submission(
            ref=order.orderRef,
            symbol=contract.symbol,
            sec_type=contract.secType,
            con_id=contract.conId,
            quantity=float(order.totalQuantity),
            state="submitting",
            order_id=order.orderId,
            client_id=order.clientId,
            perm_id=order.permId,
        )
        self._save()

    def observe(self, trade: Trade) -> None:
        order = trade.order
        entry = self.data.submissions.get(order.orderRef)
        if entry is None or order.account != self.data.account:
            return
        entry.order_id = order.orderId
        entry.client_id = order.clientId
        entry.perm_id = order.permId or trade.orderStatus.permId
        entry.status = trade.orderStatus.status
        if entry.status in {"Filled", "Cancelled", "ApiCancelled"}:
            entry.state = "terminal"
        elif entry.status in {"Submitted", "PreSubmitted"}:
            entry.state = "acknowledged"
        else:
            # PendingCancel and Inactive alone are not proof of a terminal order.
            entry.state = "unknown"
        self._save()

    def reconcile(self, trades: Sequence[Trade], fills: Sequence[Fill]) -> None:
        for trade in trades:
            self.observe(trade)
        for entry in self.data.submissions.values():
            if entry.state == "terminal":
                continue
            executions: dict[str, tuple[int, Execution]] = {}
            for fill in fills:
                execution = fill.execution
                if not (
                    execution.execId
                    and execution.acctNumber == self.data.account
                    and execution.orderRef == entry.ref
                    and fill.contract.secType == entry.sec_type
                    and (not entry.con_id or fill.contract.conId == entry.con_id)
                ):
                    continue
                # IB correction IDs differ only after the last period. Keep the
                # latest correction, never add it as another partial execution.
                prefix, separator, suffix = execution.execId.rpartition(".")
                key = prefix if separator and suffix.isdigit() else execution.execId
                revision = int(suffix) if separator and suffix.isdigit() else 0
                if key not in executions or revision > executions[key][0]:
                    executions[key] = (revision, execution)
            quantities = [float(e.shares) for _, e in executions.values()]
            if (
                quantities
                and all(math.isfinite(q) and 0 <= q < 1e100 for q in quantities)
                and sum(quantities) >= entry.quantity
            ):
                entry.state = "terminal"
                entry.status = "Filled"
        self._save()
        unresolved = [
            entry.ref
            for entry in self.data.submissions.values()
            if entry.state != "terminal"
        ]
        if unresolved:
            raise ExecutionError(
                "Unresolved prior submissions; no new orders will be sent. "
                f"Check broker orders/executions for {', '.join(unresolved)}. "
                f"Journal: {self.path}. Absence from broker history is not clearance."
            )
