from __future__ import annotations

import json
import logging
import os
import platform
import shutil
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional

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
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from alembic import command
from thetagang import log


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    config_path: Mapped[str] = mapped_column(String, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    hostname: Mapped[str] = mapped_column(String, nullable=False)
    config_text: Mapped[Optional[str]] = mapped_column(Text)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    symbol: Mapped[Optional[str]] = mapped_column(String)
    payload: Mapped[Optional[str]] = mapped_column(Text)


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
    con_id: Mapped[Optional[int]] = mapped_column(Integer)
    sec_type: Mapped[Optional[str]] = mapped_column(String)
    position: Mapped[Optional[float]] = mapped_column(Float)
    avg_cost: Mapped[Optional[float]] = mapped_column(Float)
    market_price: Mapped[Optional[float]] = mapped_column(Float)
    market_value: Mapped[Optional[float]] = mapped_column(Float)
    unrealized_pnl: Mapped[Optional[float]] = mapped_column(Float)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float)
    currency: Mapped[Optional[str]] = mapped_column(String)
    exchange: Mapped[Optional[str]] = mapped_column(String)
    multiplier: Mapped[Optional[str]] = mapped_column(String)
    expiry: Mapped[Optional[str]] = mapped_column(String)
    strike: Mapped[Optional[float]] = mapped_column(Float)
    right: Mapped[Optional[str]] = mapped_column(String)


class OrderIntent(Base):
    __tablename__ = "order_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    sec_type: Mapped[Optional[str]] = mapped_column(String)
    con_id: Mapped[Optional[int]] = mapped_column(Integer)
    exchange: Mapped[Optional[str]] = mapped_column(String)
    currency: Mapped[Optional[str]] = mapped_column(String)
    action: Mapped[Optional[str]] = mapped_column(String)
    quantity: Mapped[Optional[float]] = mapped_column(Float)
    limit_price: Mapped[Optional[float]] = mapped_column(Float)
    order_type: Mapped[Optional[str]] = mapped_column(String)
    order_ref: Mapped[Optional[str]] = mapped_column(String)
    tif: Mapped[Optional[str]] = mapped_column(String)
    payload_json: Mapped[Optional[str]] = mapped_column(Text)


class OrderRecord(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    intent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("order_intents.id"))
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    sec_type: Mapped[Optional[str]] = mapped_column(String)
    con_id: Mapped[Optional[int]] = mapped_column(Integer)
    exchange: Mapped[Optional[str]] = mapped_column(String)
    currency: Mapped[Optional[str]] = mapped_column(String)
    action: Mapped[Optional[str]] = mapped_column(String)
    quantity: Mapped[Optional[float]] = mapped_column(Float)
    limit_price: Mapped[Optional[float]] = mapped_column(Float)
    order_type: Mapped[Optional[str]] = mapped_column(String)
    order_ref: Mapped[Optional[str]] = mapped_column(String)
    order_id: Mapped[Optional[int]] = mapped_column(Integer)


class OrderStatus(Base):
    __tablename__ = "order_statuses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    order_id: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[Optional[str]] = mapped_column(String)
    filled: Mapped[Optional[float]] = mapped_column(Float)
    remaining: Mapped[Optional[float]] = mapped_column(Float)
    avg_fill_price: Mapped[Optional[float]] = mapped_column(Float)
    last_fill_price: Mapped[Optional[float]] = mapped_column(Float)
    perm_id: Mapped[Optional[int]] = mapped_column(Integer)


class ExecutionRecord(Base):
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    exec_id: Mapped[Optional[str]] = mapped_column(String, unique=True)
    order_id: Mapped[Optional[int]] = mapped_column(Integer)
    order_ref: Mapped[Optional[str]] = mapped_column(String)
    symbol: Mapped[Optional[str]] = mapped_column(String)
    side: Mapped[Optional[str]] = mapped_column(String)
    shares: Mapped[Optional[float]] = mapped_column(Float)
    price: Mapped[Optional[float]] = mapped_column(Float)
    execution_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    exchange: Mapped[Optional[str]] = mapped_column(String)


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
    open: Mapped[Optional[float]] = mapped_column(Float)
    high: Mapped[Optional[float]] = mapped_column(Float)
    low: Mapped[Optional[float]] = mapped_column(Float)
    close: Mapped[Optional[float]] = mapped_column(Float)
    volume: Mapped[Optional[float]] = mapped_column(Float)
    bar_count: Mapped[Optional[int]] = mapped_column(Integer)
    average: Mapped[Optional[float]] = mapped_column(Float)


def sqlite_db_path(db_url: str) -> Optional[Path]:
    url = make_url(db_url)
    if not url.drivername.startswith("sqlite"):
        return None
    if url.database in (None, "", ":memory:"):
        return None
    return Path(url.database)


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
    connect_args: Dict[str, Any] = {}
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
        config_text: Optional[str] = None,
    ) -> None:
        if not db_url.startswith("sqlite"):
            raise ValueError("Only sqlite database URLs are supported.")
        self.db_url = db_url
        self.config_path = config_path
        connect_args: Dict[str, Any] = {}
        if db_url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        self.engine = create_engine(db_url, future=True, connect_args=connect_args)
        self.Session = sessionmaker(bind=self.engine, future=True)
        run_migrations(db_url)
        self.dry_run = dry_run
        self.run_id = self._create_run(config_path, dry_run, config_text)

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
        self, config_path: str, dry_run: bool, config_text: Optional[str]
    ) -> int:
        version = os.getenv("THETAGANG_VERSION", "unknown")
        try:
            from importlib.metadata import version as pkg_version

            version = pkg_version("thetagang")
        except Exception:
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
        payload: Optional[Dict[str, Any]] = None,
        symbol: Optional[str] = None,
    ) -> None:
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
        except Exception as exc:
            log.warning(f"Failed to record event {event_type}: {exc}")

    def get_last_event_payload(self, event_type: str) -> Optional[Dict[str, Any]]:
        try:
            with self.session_scope() as session:
                event = (
                    session.query(Event)
                    .join(Run, Event.run_id == Run.id)
                    .filter(Event.event_type == event_type)
                    .filter(Run.config_path == self.config_path)
                    .filter(Run.dry_run.is_(False))
                    .order_by(Event.created_at.desc())
                    .first()
                )
                payload = event.payload if event else None
            if not payload:
                return None
            return json.loads(payload)
        except Exception as exc:
            log.warning(f"Failed to read event {event_type}: {exc}")
            return None

    def record_account_snapshot(self, summary: Dict[str, Any]) -> None:
        try:
            payload: Dict[str, Dict[str, Optional[str]]] = {}
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
        except Exception as exc:
            log.warning(f"Failed to record account snapshot: {exc}")

    def record_positions_snapshot(self, positions: Mapping[str, Iterable[Any]]) -> None:
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
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
        except Exception as exc:
            log.warning(f"Failed to record positions snapshot: {exc}")

    def record_order_intent(self, contract: Any, order: Any) -> Optional[int]:
        try:

            def _safe_vars(obj: Any) -> Dict[str, Any]:
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
        except Exception as exc:
            log.warning(f"Failed to record order intent: {exc}")
            return None

    def record_order(
        self, contract: Any, order: Any, intent_id: Optional[int] = None
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
        except Exception as exc:
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
        except Exception as exc:
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
                    dict(
                        run_id=self.run_id,
                        exec_id=getattr(execution, "execId", None),
                        order_id=getattr(execution, "orderId", None),
                        order_ref=getattr(execution, "orderRef", None),
                        symbol=getattr(contract, "symbol", None),
                        side=getattr(execution, "side", None),
                        shares=getattr(execution, "shares", None),
                        price=getattr(execution, "price", None),
                        execution_time=exec_time,
                        exchange=getattr(execution, "exchange", None),
                    )
                )
            if rows:
                with self.session_scope() as session:
                    stmt = sqlite_insert(ExecutionRecord).values(rows)
                    stmt = stmt.on_conflict_do_nothing(index_elements=["exec_id"])
                    session.execute(stmt)
        except Exception as exc:
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
                    dict(
                        symbol=symbol,
                        bar_time=bar_time,
                        timeframe=timeframe,
                        open=getattr(bar, "open", None),
                        high=getattr(bar, "high", None),
                        low=getattr(bar, "low", None),
                        close=getattr(bar, "close", None),
                        volume=getattr(bar, "volume", None),
                        bar_count=getattr(bar, "barCount", None),
                        average=getattr(bar, "average", None),
                    )
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
        except Exception as exc:
            log.warning(f"Failed to record historical bars: {exc}")

    def get_last_regime_rebalance_time(
        self,
        symbols: Iterable[str],
        order_ref_prefix: str,
        start_time: datetime,
    ) -> Optional[datetime]:
        with self.session_scope() as session:
            stmt = (
                select(ExecutionRecord.execution_time)
                .where(ExecutionRecord.execution_time >= start_time)
                .where(ExecutionRecord.order_ref.like(f"{order_ref_prefix}%"))
                .where(ExecutionRecord.symbol.in_(list(symbols)))
                .order_by(ExecutionRecord.execution_time.desc())
                .limit(1)
            )
            result = session.execute(stmt).scalar_one_or_none()
            return result


def _parse_bar_time(value: Any) -> Optional[datetime]:
    return _parse_datetime(value, assume_start_of_day=True)


def _parse_datetime(
    value: Any, *, assume_start_of_day: bool = False
) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if hasattr(value, "date"):
        try:
            return datetime.combine(value.date(), datetime.min.time())
        except Exception:
            return None
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            if len(raw) == 8 and assume_start_of_day:
                return datetime.strptime(raw, "%Y%m%d")
            if len(raw) in (10, 13):
                timestamp = int(raw)
                if len(raw) == 13:
                    timestamp = int(raw) / 1000
                return datetime.fromtimestamp(timestamp, timezone.utc).replace(
                    tzinfo=None
                )
        for fmt in (
            "%Y%m%d  %H:%M:%S",
            "%Y%m%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                parsed = datetime.strptime(raw, fmt)
                if fmt == "%Y-%m-%d" and not assume_start_of_day:
                    return None
                return parsed
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if isinstance(parsed, datetime) and parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return None
