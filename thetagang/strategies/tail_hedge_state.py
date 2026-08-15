from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Literal, Mapping

from thetagang.db import DataStore
from thetagang.options import contract_date_to_datetime

TAIL_HEDGE_ENTRY_ORDER_REF = "tg:tail-hedge:entry"
TAIL_HEDGE_CLOSE_ORDER_REF = "tg:tail-hedge:close"
TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX = "tg:tail-harvest"

_ORDER_REF_EPOCH = datetime(1970, 1, 1)

TailHedgeStatus = Literal["entry_enqueued", "active", "closed"]


def is_tail_reduction_ref(order_ref: Any) -> bool:
    return isinstance(order_ref, str) and (
        order_ref == TAIL_HEDGE_CLOSE_ORDER_REF
        or order_ref.startswith(f"{TAIL_HEDGE_CLOSE_ORDER_REF}:")
        or order_ref.startswith(f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:")
    )


def is_tail_order_ref(order_ref: Any) -> bool:
    return order_ref == TAIL_HEDGE_ENTRY_ORDER_REF or is_tail_reduction_ref(order_ref)


def parse_state_datetime(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.replace(tzinfo=None)


def build_tail_reduction_order_ref(
    prefix: str,
    con_id: int,
    enqueued_at: datetime | None,
) -> str:
    """Build a broker ref that identifies one persisted recovery intent."""
    parsed = parse_state_datetime(enqueued_at)
    if (
        not isinstance(prefix, str)
        or not prefix
        or type(con_id) is not int
        or con_id <= 0
        or parsed is None
    ):
        raise ValueError("Tail reduction requires a valid persisted intent")
    elapsed_microseconds = (parsed - _ORDER_REF_EPOCH) // timedelta(microseconds=1)
    return f"{prefix}:{con_id:x}:{elapsed_microseconds:x}"


@dataclass(slots=True)
class TailHedgeCohort:
    """One put purchase from entry intent through annual-budget retirement."""

    entry_id: str
    symbol: str
    status: TailHedgeStatus
    con_id: int
    expiration: str
    strike: float
    quantity: int
    entry_limit_price: float
    entered_at: datetime
    estimated_cost: float
    recovered_cost: float = 0.0
    pending_recovery_quantity: int | None = None
    pending_recovery_per_contract: float | None = None
    pending_recovery_enqueued_at: datetime | None = None
    pending_recovery_initial_quantity: int | None = None

    def __post_init__(self) -> None:
        self.entered_at = self._datetime(self.entered_at, "entry timestamp")
        if self.pending_recovery_enqueued_at is not None:
            self.pending_recovery_enqueued_at = self._datetime(
                self.pending_recovery_enqueued_at,
                "recovery timestamp",
            )
        self.strike = self._number(self.strike)
        self.entry_limit_price = self._number(self.entry_limit_price)
        self.estimated_cost = self._number(self.estimated_cost)
        self.recovered_cost = self._number(self.recovered_cost)
        if self.pending_recovery_per_contract is not None:
            self.pending_recovery_per_contract = self._number(
                self.pending_recovery_per_contract
            )
        self.validate()

    @staticmethod
    def _datetime(value: Any, description: str) -> datetime:
        if not isinstance(value, datetime):
            raise RuntimeError(f"Tail-hedge cohort has an invalid {description}")
        parsed = parse_state_datetime(value)
        assert parsed is not None
        return parsed

    @staticmethod
    def _number(value: Any) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise RuntimeError("Tail-hedge state contains an invalid cohort")
        return float(value)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> TailHedgeCohort:
        try:
            return cls(**{field: row[field] for field in cls.__dataclass_fields__})
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Tail-hedge state contains an invalid cohort") from exc

    @property
    def is_open(self) -> bool:
        return self.status != "closed"

    @property
    def net_charge(self) -> float:
        return max(0.0, self.estimated_cost - self.recovered_cost)

    @property
    def has_pending_recovery(self) -> bool:
        return self.pending_recovery_quantity is not None

    @property
    def accounted_recovery_quantity(self) -> int:
        if not self.has_pending_recovery:
            return 0
        assert self.pending_recovery_initial_quantity is not None
        assert self.pending_recovery_quantity is not None
        return max(
            0,
            self.pending_recovery_initial_quantity - self.pending_recovery_quantity,
        )

    def begin_recovery(
        self,
        *,
        quantity: int,
        proceeds_per_contract: float,
        enqueued_at: datetime,
    ) -> None:
        self.pending_recovery_quantity = quantity
        self.pending_recovery_per_contract = self._number(proceeds_per_contract)
        self.pending_recovery_enqueued_at = self._datetime(
            enqueued_at,
            "recovery timestamp",
        )
        self.pending_recovery_initial_quantity = quantity
        self.validate()

    def resize_recovery(self, quantity: int) -> None:
        if (
            not self.has_pending_recovery
            or self.accounted_recovery_quantity != 0
            or type(quantity) is not int
            or quantity <= 0
            or quantity > self.quantity
        ):
            raise RuntimeError("Tail-hedge state contains invalid recovery intent")
        self.pending_recovery_quantity = quantity
        self.pending_recovery_initial_quantity = quantity
        self.validate()

    def apply_recovery(self, quantity: int) -> float:
        if not self.has_pending_recovery:
            return 0.0
        if type(quantity) is not int or quantity < 0:
            raise ValueError("Tail-hedge recovery quantity must be non-negative")
        assert self.pending_recovery_quantity is not None
        assert self.pending_recovery_per_contract is not None
        recovered_quantity = min(quantity, self.pending_recovery_quantity)
        self.pending_recovery_quantity -= recovered_quantity
        recovery = recovered_quantity * self.pending_recovery_per_contract
        updated = min(self.estimated_cost, self.recovered_cost + recovery)
        credited = updated - self.recovered_cost
        self.recovered_cost = updated
        return credited

    def clear_recovery(self) -> None:
        self.pending_recovery_quantity = None
        self.pending_recovery_per_contract = None
        self.pending_recovery_enqueued_at = None
        self.pending_recovery_initial_quantity = None

    def close(self) -> None:
        self.status = "closed"
        self.clear_recovery()

    def validate(self) -> None:
        if (
            not isinstance(self.entry_id, str)
            or not self.entry_id
            or not isinstance(self.symbol, str)
            or not self.symbol
            or self.status not in {"entry_enqueued", "active", "closed"}
            or type(self.con_id) is not int
            or self.con_id <= 0
            or not isinstance(self.expiration, str)
            or not self.expiration
            or not math.isfinite(self.strike)
            or self.strike <= 0
            or type(self.quantity) is not int
            or self.quantity <= 0
            or not math.isfinite(self.entry_limit_price)
            or self.entry_limit_price <= 0
            or not math.isfinite(self.estimated_cost)
            or self.estimated_cost < 0
            or not math.isfinite(self.recovered_cost)
            or self.recovered_cost < 0
            or self.recovered_cost > self.estimated_cost
        ):
            raise RuntimeError("Tail-hedge state contains an invalid cohort")
        try:
            contract_date_to_datetime(self.expiration)
        except ValueError as exc:
            raise RuntimeError(
                "Tail-hedge state contains an invalid expiration"
            ) from exc

        pending = (
            self.pending_recovery_quantity,
            self.pending_recovery_per_contract,
            self.pending_recovery_enqueued_at,
            self.pending_recovery_initial_quantity,
        )
        if all(value is None for value in pending):
            return
        quantity = self.pending_recovery_quantity
        proceeds = self.pending_recovery_per_contract
        initial_quantity = self.pending_recovery_initial_quantity
        if any(value is None for value in pending) or self.status != "active":
            raise RuntimeError("Tail-hedge state contains invalid recovery intent")
        if (
            type(quantity) is not int
            or quantity <= 0
            or quantity > self.quantity
            or not isinstance(proceeds, (int, float))
            or not math.isfinite(proceeds)
            or proceeds < 0
            or type(initial_quantity) is not int
            or initial_quantity < quantity
        ):
            raise RuntimeError("Tail-hedge state contains invalid recovery intent")


@dataclass
class TailHedgeState:
    """Account-scoped cohorts used for ownership and annual-budget accounting."""

    cohorts: list[TailHedgeCohort] = field(default_factory=list)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> TailHedgeState:
        state = cls([TailHedgeCohort.from_row(row) for row in rows])
        state._validate_uniqueness()
        return state

    def to_rows(self) -> list[dict[str, Any]]:
        self.validate()
        return [asdict(cohort) for cohort in self.cohorts]

    @property
    def open_cohorts(self) -> list[TailHedgeCohort]:
        return [cohort for cohort in self.cohorts if cohort.is_open]

    @property
    def owned_con_ids(self) -> set[int]:
        return {cohort.con_id for cohort in self.open_cohorts}

    def recent_cohorts(
        self,
        now: datetime,
        *,
        days: int = 365,
    ) -> list[TailHedgeCohort]:
        cutoff = now - timedelta(days=days)
        return [cohort for cohort in self.cohorts if cohort.entered_at > cutoff]

    def find_open(self, entry_id: str, con_id: int) -> TailHedgeCohort | None:
        return next(
            (
                cohort
                for cohort in self.open_cohorts
                if cohort.entry_id == entry_id and cohort.con_id == con_id
            ),
            None,
        )

    def find_open_by_con_id(self, con_id: int) -> TailHedgeCohort | None:
        return next(
            (cohort for cohort in self.open_cohorts if cohort.con_id == con_id),
            None,
        )

    def prune_closed(self, now: datetime, *, days: int = 365) -> None:
        cutoff = now - timedelta(days=days)
        self.cohorts[:] = [
            cohort
            for cohort in self.cohorts
            if cohort.is_open or cohort.entered_at > cutoff
        ]

    def validate(self) -> None:
        for cohort in self.cohorts:
            if not isinstance(cohort, TailHedgeCohort):
                raise RuntimeError("Tail-hedge state contains invalid cohort data")
            cohort.validate()
        self._validate_uniqueness()

    def _validate_uniqueness(self) -> None:
        entry_ids: set[str] = set()
        open_con_ids: set[int] = set()
        for cohort in self.cohorts:
            if not isinstance(cohort, TailHedgeCohort):
                raise RuntimeError("Tail-hedge state contains invalid cohort data")
            if cohort.entry_id in entry_ids or (
                cohort.is_open and cohort.con_id in open_con_ids
            ):
                raise RuntimeError("Tail-hedge state contains duplicate cohorts")
            entry_ids.add(cohort.entry_id)
            if cohort.is_open:
                open_con_ids.add(cohort.con_id)


class TailHedgeStateStore:
    """Account-scoped typed SQLite persistence for the put overlay."""

    def __init__(self, data_store: DataStore, account_number: str) -> None:
        self.data_store = data_store
        self.account_number = account_number

    def load(self, *, raise_on_error: bool = True) -> TailHedgeState:
        rows = self.data_store.load_tail_hedge_entries(
            self.account_number,
            raise_on_error=raise_on_error,
        )
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("Tail-hedge state contains invalid row data")
        return TailHedgeState.from_rows(rows)

    def save(self, state: TailHedgeState) -> None:
        recorded = self.data_store.save_tail_hedge_entries(
            self.account_number,
            state.to_rows(),
        )
        if not recorded:
            raise RuntimeError("Failed to persist required tail-hedge state")

    def release_entry_submission(self, con_id: int) -> bool:
        """Release one still-pending entry that will not reach the broker."""
        state = self.load()
        cohort = state.find_open_by_con_id(con_id)
        if cohort is None or cohort.status != "entry_enqueued":
            return False
        state.cohorts.remove(cohort)
        self.save(state)
        return True

    def update_recovery_submission(
        self,
        con_id: int,
        quantity: int | None,
        *,
        live_quantity: int | None = None,
    ) -> bool:
        """Resize or release a persisted recovery intent at final submission."""
        state = self.load()
        cohort = state.find_open_by_con_id(con_id)
        if cohort is None or not cohort.has_pending_recovery:
            return False
        if quantity is None:
            cohort.clear_recovery()
        else:
            if (
                type(live_quantity) is not int
                or live_quantity <= 0
                or quantity > live_quantity
            ):
                raise RuntimeError("Tail reduction has invalid live quantity")
            # A change before broker submission cannot have come from this order,
            # so update ownership without crediting estimated sale proceeds.
            cohort.quantity = min(cohort.quantity, live_quantity)
            cohort.resize_recovery(quantity)
        self.save(state)
        return True
