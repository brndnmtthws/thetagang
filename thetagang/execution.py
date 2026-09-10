from dataclasses import asdict, dataclass
from typing import Literal, Sequence

from ib_async import Trade

ExecutionStatus = Literal["success", "working", "partial_failure", "failed", "unknown"]


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    filled: int = 0
    working: int = 0
    failed: int = 0
    unknown: int = 0
    partially_filled: int = 0

    @property
    def successful(self) -> bool:
        return self.status in {"success", "working"}

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ExecutionError(RuntimeError):
    """Execution did not complete successfully; never retry blindly."""


class OrderSubmissionError(ExecutionError):
    """A broker call failed; acceptance at the broker may be unknown."""


def execution_result(trades: Sequence[Trade]) -> ExecutionResult:
    filled = working = failed = unknown = partially_filled = 0
    for trade in trades:
        status = trade.orderStatus.status
        if status != "Filled" and trade.orderStatus.filled > 0:
            partially_filled += 1
        if status == "Filled":
            filled += 1
        elif status in {"PreSubmitted", "Submitted"}:
            working += 1
        elif status in {"Cancelled", "ApiCancelled", "Inactive"}:
            failed += 1
        else:
            unknown += 1
    outcome: ExecutionStatus = "success"
    if unknown:
        outcome = "unknown"
    elif failed:
        outcome = (
            "partial_failure" if filled or working or partially_filled else "failed"
        )
    elif working:
        outcome = "working"
    return ExecutionResult(outcome, filled, working, failed, unknown, partially_filled)
