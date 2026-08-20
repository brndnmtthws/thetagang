from __future__ import annotations

import json
import logging
import os
import platform
import shutil
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alembic.config import Config as AlembicConfig
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from alembic import command
from thetagang import log


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    config_path: Mapped[str] = mapped_column(String, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    hostname: Mapped[str] = mapped_column(String, nullable=False)
    config_text: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    symbol: Mapped[str | None] = mapped_column(String)
    payload: Mapped[str | None] = mapped_column(Text)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    con_id: Mapped[int | None] = mapped_column(Integer)
    sec_type: Mapped[str | None] = mapped_column(String)
    position: Mapped[float | None] = mapped_column(Float)
    avg_cost: Mapped[float | None] = mapped_column(Float)
    market_price: Mapped[float | None] = mapped_column(Float)
    market_value: Mapped[float | None] = mapped_column(Float)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float)
    realized_pnl: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String)
    exchange: Mapped[str | None] = mapped_column(String)
    multiplier: Mapped[str | None] = mapped_column(String)
    expiry: Mapped[str | None] = mapped_column(String)
    strike: Mapped[float | None] = mapped_column(Float)
    right: Mapped[str | None] = mapped_column(String)


class OrderIntent(Base):
    __tablename__ = "order_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    sec_type: Mapped[str | None] = mapped_column(String)
    con_id: Mapped[int | None] = mapped_column(Integer)
    exchange: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str | None] = mapped_column(String)
    action: Mapped[str | None] = mapped_column(String)
    quantity: Mapped[float | None] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float)
    order_type: Mapped[str | None] = mapped_column(String)
    order_ref: Mapped[str | None] = mapped_column(String)
    tif: Mapped[str | None] = mapped_column(String)
    payload_json: Mapped[str | None] = mapped_column(Text)


class OrderRecord(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    intent_id: Mapped[int | None] = mapped_column(ForeignKey("order_intents.id"))
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    sec_type: Mapped[str | None] = mapped_column(String)
    con_id: Mapped[int | None] = mapped_column(Integer)
    exchange: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str | None] = mapped_column(String)
    action: Mapped[str | None] = mapped_column(String)
    quantity: Mapped[float | None] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float)
    order_type: Mapped[str | None] = mapped_column(String)
    order_ref: Mapped[str | None] = mapped_column(String)
    order_id: Mapped[int | None] = mapped_column(Integer)


class OrderStatus(Base):
    __tablename__ = "order_statuses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    order_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String)
    filled: Mapped[float | None] = mapped_column(Float)
    remaining: Mapped[float | None] = mapped_column(Float)
    avg_fill_price: Mapped[float | None] = mapped_column(Float)
    last_fill_price: Mapped[float | None] = mapped_column(Float)
    perm_id: Mapped[int | None] = mapped_column(Integer)


class ExecutionRecord(Base):
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    exec_id: Mapped[str | None] = mapped_column(String, unique=True)
    account: Mapped[str | None] = mapped_column(String)
    order_id: Mapped[int | None] = mapped_column(Integer)
    order_ref: Mapped[str | None] = mapped_column(String)
    symbol: Mapped[str | None] = mapped_column(String)
    side: Mapped[str | None] = mapped_column(String)
    shares: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    execution_time: Mapped[datetime | None] = mapped_column(DateTime)
    exchange: Mapped[str | None] = mapped_column(String)


class TailHedgeEntry(Base):
    __tablename__ = "tail_hedge_entries"

    account: Mapped[str] = mapped_column(String, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    con_id: Mapped[int] = mapped_column(Integer, nullable=False)
    expiration: Mapped[str] = mapped_column(String, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_limit_price: Mapped[float] = mapped_column(Float, nullable=False)
    entered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False)
    recovered_cost: Mapped[float] = mapped_column(Float, nullable=False)
    pending_recovery_quantity: Mapped[int | None] = mapped_column(Integer)
    pending_recovery_per_contract: Mapped[float | None] = mapped_column(Float)
    pending_recovery_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime)
    pending_recovery_initial_quantity: Mapped[int | None] = mapped_column(Integer)


TAIL_HEDGE_ENTRY_FIELDS = tuple(
    column.name
    for column in TailHedgeEntry.__table__.columns
    if column.name != "account"
)


class HistoricalBar(Base):
    __tablename__ = "historical_bars"
    __table_args__ = (
        UniqueConstraint("symbol", "bar_time", "timeframe", name="uniq_bar_time"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    bar_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    timeframe: Mapped[str] = mapped_column(String, nullable=False)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    bar_count: Mapped[int | None] = mapped_column(Integer)
    average: Mapped[float | None] = mapped_column(Float)


def sqlite_db_path(db_url: str) -> Path | None:
    try:
        url = make_url(db_url)
    except ArgumentError:
        return None
    if url.drivername not in {"sqlite", "sqlite+pysqlite"}:
        return None
    try:
        authority = (url.username, url.password, url.host, url.port)
    except ValueError:
        return None
    if any(component is not None for component in authority):
        return None
    database = url.database
    if database in (None, "", ":memory:"):
        return None
    if database.lower().startswith("file:"):
        return None
    if "uri" in url.query:
        return None
    if str(url.query.get("mode", "")).lower() == "memory":
        return None
    return Path(database)


def is_persistent_sqlite_url(db_url: str) -> bool:
    """Return whether a SQLAlchemy URL names file-backed SQLite storage."""
    return sqlite_db_path(db_url) is not None


def make_alembic_config(db_url: str) -> AlembicConfig:
    base_dir = Path(__file__).resolve().parent.parent
    alembic_cfg = AlembicConfig(str(base_dir / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_cfg.set_main_option("script_location", str(base_dir / "alembic"))
    configure_logger = (
        logging.getLogger("thetagang.main").getEffectiveLevel() <= logging.INFO
    )
    alembic_cfg.attributes["configure_logger"] = configure_logger
    return alembic_cfg


def _run_alembic_upgrade(alembic_cfg: AlembicConfig, db_url: str) -> None:
    connect_args: dict[str, Any] = {}
    if db_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    engine = create_engine(db_url, future=True, connect_args=connect_args)
    with engine.connect() as connection:
        alembic_cfg.attributes["connection"] = connection
        command.upgrade(alembic_cfg, "head")


def run_migrations(db_url: str) -> None:
    alembic_cfg = make_alembic_config(db_url)
    sqlite_path = sqlite_db_path(db_url)

    backup_path = None
    migration_url = db_url
    temp_path = None
    if sqlite_path:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        if sqlite_path.exists():
            backup_path = sqlite_path.with_suffix(f"{sqlite_path.suffix}.bak")
            shutil.copy2(sqlite_path, backup_path)
        else:
            temp_path = sqlite_path.with_suffix(f"{sqlite_path.suffix}.tmp")
            migration_url = f"sqlite:///{temp_path}"

    try:
        _run_alembic_upgrade(alembic_cfg, migration_url)
        if sqlite_path and temp_path:
            if sqlite_path.exists():
                sqlite_path.unlink()
            temp_path.replace(sqlite_path)
    except Exception:
        if sqlite_path and backup_path and backup_path.exists():
            shutil.copy2(backup_path, sqlite_path)
        if temp_path and temp_path.exists():
            temp_path.unlink()
        raise
    finally:
        if backup_path and backup_path.exists():
            backup_path.unlink()


class DataStore:
    def __init__(
        self,
        db_url: str,
        config_path: str,
        dry_run: bool,
        config_text: str | None = None,
    ) -> None:
        if not db_url.startswith("sqlite"):
            raise ValueError("Only sqlite database URLs are supported.")
        self.db_url = db_url
        raw_config_path = str(config_path)
        canonical_config_path = Path(raw_config_path).expanduser().resolve()
        self.config_path = str(canonical_config_path)
        config_path_aliases = {
            self.config_path,
            raw_config_path,
            str(Path(raw_config_path).expanduser()),
        }
        try:
            cwd_relative_path = os.path.relpath(
                canonical_config_path,
                start=Path.cwd().resolve(),
            )
        except ValueError:
            pass
        else:
            config_path_aliases.add(cwd_relative_path)
            config_path_aliases.add(f".{os.sep}{cwd_relative_path}")
        self._config_path_aliases = tuple(sorted(config_path_aliases))
        self._dry_run_event_overlay: dict[tuple[str, str | None], str | None] = {}
        self._dry_run_tail_hedge_overlay: dict[str, list[dict[str, Any]]] = {}
        connect_args: dict[str, Any] = {}
        if db_url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        self.engine = create_engine(db_url, future=True, connect_args=connect_args)
        self.Session = sessionmaker(bind=self.engine, future=True)
        run_migrations(db_url)
        self.dry_run = dry_run
        self.run_id = self._create_run(self.config_path, dry_run, config_text)

    @contextmanager
    def session_scope(self) -> Iterator[Any]:
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _create_run(
        self, config_path: str, dry_run: bool, config_text: str | None
    ) -> int:
        version = os.getenv("THETAGANG_VERSION", "unknown")
        try:
            from importlib.metadata import version as pkg_version

            version = pkg_version("thetagang")
        except Exception:  # noqa: BLE001, S110
            pass
        hostname = platform.node() or "unknown"

        with self.session_scope() as session:
            run = Run(
                config_path=config_path,
                dry_run=dry_run,
                version=version,
                hostname=hostname,
                config_text=config_text,
            )
            session.add(run)
            session.flush()
            return int(run.id)

    def record_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        symbol: str | None = None,
    ) -> bool:
        try:
            payload_json = json.dumps(payload, default=str) if payload else None
            with self.session_scope() as session:
                session.add(
                    Event(
                        run_id=self.run_id,
                        event_type=event_type,
                        symbol=symbol,
                        payload=payload_json,
                    )
                )
            if self.dry_run:
                self._dry_run_event_overlay[(event_type, symbol)] = payload_json
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Failed to record event {event_type}: {exc}")
            return False

    def get_last_event_payload(
        self,
        event_type: str,
        *,
        symbol: str | None = None,
        config_scoped: bool = True,
        exclude_current_run: bool = False,
        raise_on_error: bool = False,
    ) -> dict[str, Any] | None:
        try:
            overlay_key = (event_type, symbol)
            if (
                self.dry_run
                and not exclude_current_run
                and overlay_key in self._dry_run_event_overlay
            ):
                payload = self._dry_run_event_overlay[overlay_key]
                if not payload:
                    return None
                decoded = json.loads(payload)
                if not isinstance(decoded, dict):
                    raise ValueError(f"Event {event_type} payload is not an object")
                return decoded

            with self.session_scope() as session:
                query = (
                    session.query(Event)
                    .join(Run, Event.run_id == Run.id)
                    .filter(Event.event_type == event_type)
                    .filter(Run.dry_run.is_(False))
                )
                if symbol is not None:
                    query = query.filter(Event.symbol == symbol)
                if config_scoped:
                    query = query.filter(Run.config_path.in_(self._config_path_aliases))
                if exclude_current_run:
                    query = query.filter(Event.run_id != self.run_id)
                event = query.order_by(Event.id.desc()).first()
                payload = event.payload if event else None
            if not payload:
                return None
            decoded = json.loads(payload)
            if not isinstance(decoded, dict):
                raise ValueError(  # noqa: TRY004
                    f"Event {event_type} payload is not an object"
                )
            return decoded
        except Exception as exc:
            log.warning(f"Failed to read event {event_type}: {exc}")
            if raise_on_error:
                raise RuntimeError(f"Failed to read event {event_type}") from exc
            return None

    def discard_current_run_events(self, event_types: Iterable[str]) -> None:
        """Discard committed state events and overlays for the active run."""
        selected = {event_type for event_type in event_types if event_type}
        if not selected:
            return
        try:
            with self.session_scope() as session:
                (
                    session.query(Event)
                    .filter(Event.run_id == self.run_id)
                    .filter(Event.event_type.in_(selected))
                    .delete(synchronize_session=False)
                )
            for key in list(self._dry_run_event_overlay):
                if key[0] in selected:
                    self._dry_run_event_overlay.pop(key, None)
        except Exception as exc:
            raise RuntimeError("Failed to discard active-run state") from exc

    @staticmethod
    def _tail_hedge_entry_dict(entry: TailHedgeEntry) -> dict[str, Any]:
        return {field: getattr(entry, field) for field in TAIL_HEDGE_ENTRY_FIELDS}

    def load_tail_hedge_entries(
        self,
        account: str,
        *,
        raise_on_error: bool = False,
    ) -> list[dict[str, Any]]:
        """Load the current account-scoped tail cohorts from typed SQLite rows."""
        try:
            if self.dry_run and account in self._dry_run_tail_hedge_overlay:
                return [
                    dict(entry) for entry in self._dry_run_tail_hedge_overlay[account]
                ]

            with self.session_scope() as session:
                entries = (
                    session.execute(
                        select(TailHedgeEntry)
                        .where(TailHedgeEntry.account == account)
                        .order_by(TailHedgeEntry.entered_at, TailHedgeEntry.entry_id)
                    )
                    .scalars()
                    .all()
                )
                return [self._tail_hedge_entry_dict(entry) for entry in entries]
        except Exception as exc:
            log.warning(f"Failed to read tail-hedge state: {exc}")
            if raise_on_error:
                raise RuntimeError("Failed to read tail-hedge state") from exc
            return []

    def save_tail_hedge_entries(
        self,
        account: str,
        entries: Iterable[Mapping[str, Any]],
    ) -> bool:
        """Atomically replace one account's durable tail-cohort rows."""
        try:
            current = [
                {field: entry[field] for field in TAIL_HEDGE_ENTRY_FIELDS}
                for entry in entries
            ]
            entry_ids = [entry["entry_id"] for entry in current]
            if len(entry_ids) != len(set(entry_ids)):
                raise ValueError("Tail-hedge cohort entry IDs must be unique")
            current.sort(key=lambda entry: (entry["entered_at"], entry["entry_id"]))
            if self.dry_run:
                self._dry_run_tail_hedge_overlay[account] = current
                return True

            with self.session_scope() as session:
                session.query(TailHedgeEntry).filter(
                    TailHedgeEntry.account == account
                ).delete(synchronize_session=False)
                session.add_all(
                    TailHedgeEntry(
                        account=account,
                        **entry,
                    )
                    for entry in current
                )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Failed to persist tail-hedge state: {exc}")
            return False

    def record_account_snapshot(self, summary: dict[str, Any]) -> None:
        try:
            payload: dict[str, dict[str, str | None]] = {}
            for key, value in summary.items():
                payload[key] = {
                    "value": getattr(value, "value", None),
                    "currency": getattr(value, "currency", None),
                }
            with self.session_scope() as session:
                session.add(
                    AccountSnapshot(
                        run_id=self.run_id,
                        summary_json=json.dumps(payload, default=str),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Failed to record account snapshot: {exc}")

    def record_positions_snapshot(self, positions: Mapping[str, Iterable[Any]]) -> None:
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            rows = []
            for symbol, items in positions.items():
                for position in items:
                    contract = getattr(position, "contract", None)
                    rows.append(
                        PositionSnapshot(
                            run_id=self.run_id,
                            created_at=now,
                            symbol=symbol,
                            con_id=getattr(contract, "conId", None),
                            sec_type=getattr(contract, "secType", None),
                            position=getattr(position, "position", None),
                            avg_cost=getattr(position, "averageCost", None),
                            market_price=getattr(position, "marketPrice", None),
                            market_value=getattr(position, "marketValue", None),
                            unrealized_pnl=getattr(position, "unrealizedPNL", None),
                            realized_pnl=getattr(position, "realizedPNL", None),
                            currency=getattr(contract, "currency", None),
                            exchange=getattr(contract, "exchange", None),
                            multiplier=getattr(contract, "multiplier", None),
                            expiry=getattr(
                                contract, "lastTradeDateOrContractMonth", None
                            ),
                            strike=getattr(contract, "strike", None),
                            right=getattr(contract, "right", None),
                        )
                    )
            if rows:
                with self.session_scope() as session:
                    session.add_all(rows)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Failed to record positions snapshot: {exc}")

    def record_order_intent(self, contract: Any, order: Any) -> int | None:
        try:

            def _safe_vars(obj: Any) -> dict[str, Any]:
                try:
                    return dict(vars(obj))
                except TypeError:
                    return {"repr": repr(obj)}

            payload = {
                "contract": _safe_vars(contract),
                "order": _safe_vars(order),
            }
            with self.session_scope() as session:
                intent = OrderIntent(
                    run_id=self.run_id,
                    dry_run=self.dry_run,
                    symbol=getattr(contract, "symbol", "") or "",
                    sec_type=getattr(contract, "secType", None),
                    con_id=getattr(contract, "conId", None),
                    exchange=getattr(contract, "exchange", None),
                    currency=getattr(contract, "currency", None),
                    action=getattr(order, "action", None),
                    quantity=getattr(order, "totalQuantity", None),
                    limit_price=getattr(order, "lmtPrice", None),
                    order_type=getattr(order, "orderType", None),
                    order_ref=getattr(order, "orderRef", None),
                    tif=getattr(order, "tif", None),
                    payload_json=json.dumps(payload, default=str),
                )
                session.add(intent)
                session.flush()
                return int(intent.id)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Failed to record order intent: {exc}")
            return None

    def record_order(
        self, contract: Any, order: Any, intent_id: int | None = None
    ) -> None:
        try:
            with self.session_scope() as session:
                session.add(
                    OrderRecord(
                        run_id=self.run_id,
                        intent_id=intent_id,
                        symbol=getattr(contract, "symbol", "") or "",
                        sec_type=getattr(contract, "secType", None),
                        con_id=getattr(contract, "conId", None),
                        exchange=getattr(contract, "exchange", None),
                        currency=getattr(contract, "currency", None),
                        action=getattr(order, "action", None),
                        quantity=getattr(order, "totalQuantity", None),
                        limit_price=getattr(order, "lmtPrice", None),
                        order_type=getattr(order, "orderType", None),
                        order_ref=getattr(order, "orderRef", None),
                        order_id=getattr(order, "orderId", None),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Failed to record order: {exc}")

    def record_order_status(self, trade: Any) -> None:
        try:
            status = getattr(trade, "orderStatus", None)
            order = getattr(trade, "order", None)
            with self.session_scope() as session:
                session.add(
                    OrderStatus(
                        run_id=self.run_id,
                        order_id=getattr(order, "orderId", None),
                        status=getattr(status, "status", None),
                        filled=getattr(status, "filled", None),
                        remaining=getattr(status, "remaining", None),
                        avg_fill_price=getattr(status, "avgFillPrice", None),
                        last_fill_price=getattr(status, "lastFillPrice", None),
                        perm_id=getattr(order, "permId", None),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Failed to record order status: {exc}")

    def record_executions(self, fills: Iterable[Any]) -> None:
        try:
            rows = []
            for fill in fills:
                execution = getattr(fill, "execution", None)
                contract = getattr(fill, "contract", None)
                exec_time_raw = getattr(fill, "time", None) or getattr(
                    execution, "time", None
                )
                exec_time = _parse_datetime(exec_time_raw, assume_start_of_day=True)
                rows.append(
                    {
                        "run_id": self.run_id,
                        "exec_id": getattr(execution, "execId", None),
                        "account": getattr(execution, "acctNumber", None),
                        "order_id": getattr(execution, "orderId", None),
                        "order_ref": getattr(execution, "orderRef", None),
                        "symbol": getattr(contract, "symbol", None),
                        "side": getattr(execution, "side", None),
                        "shares": getattr(execution, "shares", None),
                        "price": getattr(execution, "price", None),
                        "execution_time": exec_time,
                        "exchange": getattr(execution, "exchange", None),
                    }
                )
            if rows:
                with self.session_scope() as session:
                    stmt = sqlite_insert(ExecutionRecord).values(rows)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["exec_id"],
                        set_={
                            "account": func.coalesce(
                                stmt.excluded.account,
                                ExecutionRecord.account,
                            ),
                        },
                    )
                    session.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Failed to record executions: {exc}")

    def record_historical_bars(
        self, symbol: str, timeframe: str, bars: Iterable[Any]
    ) -> None:
        try:
            rows = []
            for bar in bars:
                bar_date = getattr(bar, "date", None)
                bar_time = _parse_bar_time(bar_date)
                if bar_time is None:
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "bar_time": bar_time,
                        "timeframe": timeframe,
                        "open": getattr(bar, "open", None),
                        "high": getattr(bar, "high", None),
                        "low": getattr(bar, "low", None),
                        "close": getattr(bar, "close", None),
                        "volume": getattr(bar, "volume", None),
                        "bar_count": getattr(bar, "barCount", None),
                        "average": getattr(bar, "average", None),
                    }
                )
            if rows:
                with self.session_scope() as session:
                    stmt = sqlite_insert(HistoricalBar).values(rows)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["symbol", "bar_time", "timeframe"],
                        set_={
                            "open": stmt.excluded.open,
                            "high": stmt.excluded.high,
                            "low": stmt.excluded.low,
                            "close": stmt.excluded.close,
                            "volume": stmt.excluded.volume,
                            "bar_count": stmt.excluded.bar_count,
                            "average": stmt.excluded.average,
                        },
                    )
                    session.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Failed to record historical bars: {exc}")

    def get_historical_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Any]:
        with self.session_scope() as session:
            rows = (
                session.execute(
                    select(HistoricalBar)
                    .where(HistoricalBar.symbol == symbol)
                    .where(HistoricalBar.timeframe == timeframe)
                    .where(HistoricalBar.bar_time >= start_time)
                    .where(HistoricalBar.bar_time <= end_time)
                    .order_by(HistoricalBar.bar_time.asc())
                )
                .scalars()
                .all()
            )
            return [
                SimpleNamespace(
                    date=row.bar_time,
                    open=row.open,
                    high=row.high,
                    low=row.low,
                    close=row.close,
                    volume=row.volume,
                    barCount=row.bar_count,
                    average=row.average,
                )
                for row in rows
            ]

    def get_last_regime_rebalance_time(
        self,
        symbols: Iterable[str],
        order_ref_prefix: str,
        start_time: datetime,
        account: str,
        *,
        include_legacy_unscoped: bool = False,
    ) -> datetime | None:
        symbols = list(symbols)
        with self.session_scope() as session:
            base_stmt = (
                select(ExecutionRecord.execution_time)
                .where(ExecutionRecord.execution_time >= start_time)
                .where(ExecutionRecord.order_ref.like(f"{order_ref_prefix}%"))
                .where(ExecutionRecord.symbol.in_(symbols))
            )
            scoped_stmt = (
                base_stmt.where(ExecutionRecord.account == account)
                .order_by(ExecutionRecord.execution_time.desc())
                .limit(1)
            )
            scoped = session.execute(scoped_stmt).scalar_one_or_none()
            if scoped is not None or not include_legacy_unscoped:
                return scoped
            legacy_stmt = (
                base_stmt.where(ExecutionRecord.account.is_(None))
                .order_by(ExecutionRecord.execution_time.desc())
                .limit(1)
            )
            return session.execute(legacy_stmt).scalar_one_or_none()


def _parse_bar_time(value: Any) -> datetime | None:
    return _parse_datetime(value, assume_start_of_day=True)


def _parse_datetime(
    value: Any, *, assume_start_of_day: bool = False
) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), UTC).replace(tzinfo=None)
    if hasattr(value, "date"):
        try:
            return datetime.combine(value.date(), datetime.min.time(), UTC).replace(
                tzinfo=None
            )
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            if len(raw) == 8 and assume_start_of_day:
                return (
                    datetime.strptime(raw, "%Y%m%d")
                    .replace(tzinfo=UTC)
                    .replace(tzinfo=None)
                )
            if len(raw) in (10, 13):
                timestamp = int(raw)
                if len(raw) == 13:
                    timestamp = int(raw) / 1000
                return datetime.fromtimestamp(timestamp, UTC).replace(tzinfo=None)
        for fmt in (
            "%Y%m%d  %H:%M:%S",
            "%Y%m%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                parsed = datetime.strptime(raw, fmt).replace(tzinfo=UTC)
                if fmt == "%Y-%m-%d" and not assume_start_of_day:
                    return None
                return parsed.replace(tzinfo=None)
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))  # noqa: FURB162
        except ValueError:
            return None
        if isinstance(parsed, datetime) and parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    return None
