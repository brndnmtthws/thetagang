from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from thetagang import log
from thetagang.db import DataStore
from thetagang.options import contract_date_to_datetime

TAIL_HEDGE_STATE_EVENT = "tail_hedge_state"
TAIL_HEDGE_STATE_SCHEMA_VERSION = 1
TAIL_HEDGE_STATE_STRATEGY = "long_put"
TAIL_HEDGE_ENTRY_ORDER_REF = "tg:tail-hedge:entry"
TAIL_HEDGE_CLOSE_ORDER_REF = "tg:tail-hedge:close"
TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX = "tg:tail-harvest"


def is_tail_reduction_ref(order_ref: Any) -> bool:
    return order_ref == TAIL_HEDGE_CLOSE_ORDER_REF or (
        isinstance(order_ref, str)
        and order_ref.startswith(f"{TAIL_HEDGE_HARVEST_ORDER_REF_PREFIX}:")
    )


def is_tail_order_ref(order_ref: Any) -> bool:
    return order_ref == TAIL_HEDGE_ENTRY_ORDER_REF or is_tail_reduction_ref(order_ref)


def parse_state_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return None


@dataclass
class TailHedgeState:
    """The durable facts needed to own puts and enforce the annual budget."""

    tranches: list[dict[str, Any]] = field(default_factory=list)
    entry_history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def owned_con_ids(self) -> set[int]:
        return {int(tranche["con_id"]) for tranche in self.tranches}

    def recent_entries(
        self,
        now: datetime,
        *,
        days: int = 365,
    ) -> list[dict[str, Any]]:
        cutoff = now - timedelta(days=days)
        return [
            entry
            for entry in self.entry_history
            if (entered_at := parse_state_datetime(entry.get("entered_at"))) is not None
            and entered_at >= cutoff
        ]

    def roll_entry_history(self, now: datetime, *, days: int = 365) -> None:
        active_entry_ids = {str(tranche["entry_id"]) for tranche in self.tranches}
        recent_entry_ids = {
            str(entry["entry_id"]) for entry in self.recent_entries(now, days=days)
        }
        retained_ids = active_entry_ids | recent_entry_ids
        self.entry_history[:] = [
            entry
            for entry in self.entry_history
            if str(entry.get("entry_id")) in retained_ids
        ]


class TailHedgeStateStore:
    """Account-scoped persistence and validation for the put overlay."""

    def __init__(
        self,
        data_store: DataStore,
        account_number: str,
        *,
        now_provider: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.data_store = data_store
        self.account_number = account_number
        self._now = now_provider

    def load(self, *, raise_on_error: bool = True) -> TailHedgeState:
        payload = self.data_store.get_last_event_payload(
            TAIL_HEDGE_STATE_EVENT,
            symbol=self.account_number,
            config_scoped=False,
            raise_on_error=raise_on_error,
        )
        if payload is None:
            return TailHedgeState()
        return self._parse(payload)

    def save(
        self,
        state: TailHedgeState,
        status: str,
        *,
        persistence_required: bool = True,
        **metadata: Any,
    ) -> dict[str, Any]:
        payload = {
            **metadata,
            "schema_version": TAIL_HEDGE_STATE_SCHEMA_VERSION,
            "strategy": TAIL_HEDGE_STATE_STRATEGY,
            "account": self.account_number,
            "status": status,
            "state_recorded_at": self._now(),
            "tranches": state.tranches,
            "entry_history": state.entry_history,
        }
        recorded = self.data_store.record_event(
            TAIL_HEDGE_STATE_EVENT,
            payload,
            symbol=self.account_number,
        )
        if not recorded and persistence_required:
            raise RuntimeError("Failed to persist required tail-hedge state")
        if not recorded:
            log.warning(
                "Failed to persist tail-hedge "
                f"{status} state; continuing with risk-reducing management."
            )
        return payload

    def _parse(self, payload: dict[str, Any]) -> TailHedgeState:
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != TAIL_HEDGE_STATE_SCHEMA_VERSION
            or payload.get("strategy") != TAIL_HEDGE_STATE_STRATEGY
        ):
            raise RuntimeError("Tail-hedge state has an invalid schema")
        if payload.get("account") != self.account_number:
            raise RuntimeError("Tail-hedge state belongs to a different account")

        raw_tranches = self._collection(payload, "tranches")
        raw_history = self._collection(payload, "entry_history")
        tranches = self._parse_tranches(raw_tranches)
        entry_history = self._parse_history(raw_history)

        tranche_symbols = {
            str(tranche["entry_id"]): str(tranche["symbol"]) for tranche in tranches
        }
        history_symbols = {
            str(entry["entry_id"]): str(entry["symbol"]) for entry in entry_history
        }
        if not tranche_symbols.keys() <= history_symbols.keys():
            raise RuntimeError("Tail-hedge state is missing tranche entry history")
        if any(
            history_symbols[entry_id] != symbol
            for entry_id, symbol in tranche_symbols.items()
        ):
            raise RuntimeError("Tail-hedge state has mismatched tranche ownership")

        return TailHedgeState(tranches=tranches, entry_history=entry_history)

    @staticmethod
    def _collection(
        payload: dict[str, Any],
        key: str,
    ) -> list[dict[str, Any]]:
        value = payload.get(key)
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise RuntimeError(f"Tail-hedge state has invalid {key} data")
        return [dict(item) for item in value]

    @staticmethod
    def _parse_tranches(
        raw_tranches: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        entry_ids: set[str] = set()
        con_ids: set[int] = set()
        for tranche in raw_tranches:
            entry_id = tranche.get("entry_id")
            symbol = tranche.get("symbol")
            con_id = tranche.get("con_id")
            expiration = tranche.get("expiration")
            quantity = tranche.get("quantity")
            entry_limit_price = tranche.get("entry_limit_price")
            if (
                not isinstance(entry_id, str)
                or not entry_id
                or not isinstance(symbol, str)
                or not symbol
                or type(con_id) is not int
                or con_id <= 0
                or not isinstance(expiration, str)
                or not expiration
                or type(quantity) is not int
                or quantity <= 0
                or isinstance(entry_limit_price, bool)
                or not isinstance(entry_limit_price, (int, float))
                or not math.isfinite(float(entry_limit_price))
                or float(entry_limit_price) <= 0
                or tranche.get("status") not in {"entry_enqueued", "active"}
            ):
                raise RuntimeError("Tail-hedge state contains an invalid tranche")
            try:
                contract_date_to_datetime(expiration)
            except ValueError as exc:
                raise RuntimeError(
                    "Tail-hedge state contains an invalid expiration"
                ) from exc
            if entry_id in entry_ids or con_id in con_ids:
                raise RuntimeError("Tail-hedge state contains duplicate tranches")
            entry_ids.add(entry_id)
            con_ids.add(con_id)
        return raw_tranches

    @staticmethod
    def _parse_history(
        raw_history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        entry_ids: set[str] = set()
        for entry in raw_history:
            entry_id = entry.get("entry_id")
            symbol = entry.get("symbol")
            entered_at = parse_state_datetime(entry.get("entered_at"))
            raw_cost = entry.get("estimated_cost")
            if isinstance(raw_cost, bool) or not isinstance(
                raw_cost, (int, float, str)
            ):
                raise RuntimeError("Tail-hedge state contains invalid entry history")
            try:
                estimated_cost = float(raw_cost)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Tail-hedge state contains invalid entry history"
                ) from exc
            if (
                not isinstance(entry_id, str)
                or not entry_id
                or not isinstance(symbol, str)
                or not symbol
                or entry_id in entry_ids
                or entered_at is None
                or not math.isfinite(estimated_cost)
                or estimated_cost < 0
            ):
                raise RuntimeError("Tail-hedge state contains invalid entry history")
            entry_ids.add(entry_id)
        return raw_history
