"""Local, read-only execution review. This module never imports the trading runner."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit

import click
import tomlkit

ASSETS = Path(__file__).with_name("dashboard_assets")
TABLES = {
    "runs",
    "events",
    "account_snapshots",
    "position_snapshots",
    "order_intents",
    "orders",
    "order_statuses",
    "executions",
}
ACCOUNT_KEYS = {
    "NetLiquidation",
    "TotalCashValue",
    "BuyingPower",
    "ExcessLiquidity",
    "InitMarginReq",
    "MaintMarginReq",
    "Cushion",
}
RUN_LABELS = {
    "success": "执行完成",
    "working": "结束时仍有挂单",
    "failed": "执行失败",
    "partial_failure": "部分失败",
    "unknown": "结果待核实",
    "skipped": "未进入交易时段",
    "unrecorded": "未记录结束",
}
ORDER_LABELS = {
    "Filled": "全部成交",
    "Submitted": "已提交",
    "PreSubmitted": "券商预处理",
    "PendingSubmit": "待提交确认",
    "PendingCancel": "撤单待确认",
    "Cancelled": "已撤销",
    "ApiCancelled": "已撤销",
    "Inactive": "未激活 / 拒绝",
    "intent": "已规划 · 提交未记录",
    "unknown": "提交结果待核实",
}


def finite(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def decode(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def utc(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def clean(value: Any) -> Any:
    """SQLite and older JSON snapshots can contain non-finite numbers."""
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def portfolio_settings(text: str | None) -> dict[str, Any]:
    """Allowlist strategy inputs; never send config text or broker credentials."""
    try:
        config = tomlkit.parse(text or "").unwrap()
        runtime = config.get("runtime", config)
        symbols = config.get("portfolio", config).get("symbols", {})
        wheel = config.get("strategies", {}).get("wheel", {})
        margin = wheel.get("risk", {}).get("margin_usage")
        if margin is None:
            margin = runtime.get("account", {}).get("margin_usage")
        total_parts = sum(finite(v.get("parts")) or 0 for v in symbols.values())
        weights = {}
        for name, item in symbols.items():
            weight = finite(item.get("weight"))
            if weight is None and total_parts > 0:
                weight = (finite(item.get("parts")) or 0) / total_parts
            weights[name] = weight
        return {"margin_usage": finite(margin), "weights": weights}
    except (TypeError, ValueError, AttributeError):
        return {"margin_usage": None, "weights": {}}


class DashboardStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        # mode=ro prevents accidental creation, migration and all database writes.
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def rows(conn: sqlite3.Connection, table: str, run_id: int) -> list[dict[str, Any]]:
        if table not in TABLES:
            raise ValueError("Unknown table")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            return []
        return [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM {table} WHERE run_id=? ORDER BY id", (run_id,)
            )
        ]

    def run_summary(
        self, conn: sqlite3.Connection, row: dict[str, Any]
    ) -> dict[str, Any]:
        run_id = row["id"]
        events = self.rows(conn, "events", run_id)
        ends = [e for e in events if e["event_type"] == "run_end"]
        end = decode(ends[-1]["payload"]) if ends else {}
        state = end.get("status", "success" if end.get("success") else "unrecorded")
        if state not in RUN_LABELS:
            state = "unknown"
        snapshots = self.rows(conn, "account_snapshots", run_id)
        account = decode(snapshots[-1]["summary_json"]) if snapshots else {}
        started = utc(row["started_at"])
        finished = utc(ends[-1]["created_at"]) if ends else None
        duration = None
        if started and finished:
            duration = max(
                0,
                (
                    datetime.fromisoformat(finished) - datetime.fromisoformat(started)
                ).total_seconds(),
            )
        return {
            "id": run_id,
            "started_at": started,
            "ended_at": finished,
            "duration": duration,
            "dry_run": bool(row["dry_run"]),
            "mode": "预演" if row["dry_run"] else "可下单运行",
            "status": state,
            "status_label": RUN_LABELS[state],
            "error_type": end.get("error_type"),
            "orders_count": len(self.rows(conn, "orders", run_id)),
            "intents_count": len(self.rows(conn, "order_intents", run_id)),
            "positions_count": len(self.rows(conn, "position_snapshots", run_id)),
            "net_liquidation": finite(account.get("NetLiquidation", {}).get("value")),
        }

    def overview(self, page: int = 1, mode: str = "all") -> dict[str, Any]:
        with self.read() as conn:
            where = " WHERE dry_run=?" if mode in {"preview", "trade"} else ""
            args: tuple[Any, ...] = (mode == "preview",) if where else ()
            total = conn.execute("SELECT count(*) FROM runs" + where, args).fetchone()[
                0
            ]
            page = min(max(1, page), max(1, math.ceil(total / 30)))
            rows = conn.execute(
                "SELECT * FROM runs" + where + " ORDER BY id DESC LIMIT 30 OFFSET ?",
                (*args, (page - 1) * 30),
            ).fetchall()
            return {
                "runs": [self.run_summary(conn, dict(row)) for row in rows],
                "total": total,
                "page": page,
                "pages": max(1, math.ceil(total / 30)),
                "read_at": datetime.now(timezone.utc).isoformat(),
            }

    def detail(self, run_id: int) -> dict[str, Any]:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise LookupError("这次运行不存在")
            run = self.run_summary(conn, dict(row))
            snapshots = self.rows(conn, "account_snapshots", run_id)
            snapshot = decode(snapshots[-1]["summary_json"]) if snapshots else {}
            account = {
                key: {
                    "value": finite(snapshot.get(key, {}).get("value")),
                    "currency": snapshot.get(key, {}).get("currency"),
                }
                for key in ACCOUNT_KEYS
            }
            positions = self.rows(conn, "position_snapshots", run_id)
            # Show only the latest snapshot, not the sum of repeated snapshots.
            latest = max((p["created_at"] for p in positions), default=None)
            positions = [p for p in positions if p["created_at"] == latest]
            for position in positions:
                position["created_at"] = utc(position["created_at"])
            events = self.rows(conn, "events", run_id)
            decisions = []
            for event in events:
                if event["event_type"] == "strategy_decision":
                    payload = decode(event["payload"])
                    decisions.append(
                        {
                            "symbol": event["symbol"],
                            "time": utc(event["created_at"]),
                            **{
                                key: payload.get(key)
                                for key in (
                                    "stage",
                                    "outcome",
                                    "reason",
                                    "metrics",
                                )
                            },
                        }
                    )
            return clean(
                {
                    "run": run,
                    "account": account,
                    "snapshot_at": utc(snapshots[-1]["created_at"])
                    if snapshots
                    else None,
                    "positions": positions,
                    "settings": portfolio_settings(row["config_text"]),
                    "orders": self.orders(conn, run_id),
                    "decisions": decisions,
                    # Executions are observed in this run, not necessarily created by it.
                    "observed_executions": [
                        {
                            k: utc(v) if k in {"created_at", "execution_time"} else v
                            for k, v in fill.items()
                        }
                        for fill in self.rows(conn, "executions", run_id)
                    ],
                    "events": [
                        {
                            "type": e["event_type"],
                            "symbol": e["symbol"],
                            "time": utc(e["created_at"]),
                        }
                        for e in events
                    ],
                    "read_at": datetime.now(timezone.utc).isoformat(),
                }
            )

    def orders(self, conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
        intents = self.rows(conn, "order_intents", run_id)
        submitted = self.rows(conn, "orders", run_id)
        statuses = self.rows(conn, "order_statuses", run_id)
        result = []
        matched = set()
        intent_ids = {i["id"] for i in intents}
        intent_refs = {i["order_ref"] for i in intents if i.get("order_ref")}
        records = [(i, True) for i in intents] + [
            (o, False)
            for o in submitted
            if o.get("intent_id") not in intent_ids
            and o.get("order_ref") not in intent_refs
        ]
        for intent, is_intent in records:
            ref = intent.get("order_ref")
            linked = (
                [
                    o
                    for o in submitted
                    if o.get("intent_id") == intent["id"]
                    or (ref and o.get("order_ref") == ref)
                ]
                if is_intent
                else [intent]
            )
            order = linked[-1] if linked else intent
            ref = ref or order.get("order_ref")
            identity = ref or f"{'order' if linked else 'intent'}:{order['id']}"
            if identity in matched:
                continue
            matched.add(identity)
            order_id = order.get("order_id") if linked else None
            # A reused API orderId must never join another run/account's history.
            states = [
                s
                for s in statuses
                if order_id is not None and s["order_id"] == order_id
            ]
            perm_ids = {s["perm_id"] for s in states if s.get("perm_id")}
            if len(perm_ids) > 1:
                # Historical status schema lacks clientId. Refuse an ambiguous ID.
                states = []
            if len(perm_ids) == 1:
                later = conn.execute(
                    "SELECT * FROM order_statuses WHERE perm_id=? AND created_at>? ORDER BY id",
                    (next(iter(perm_ids)), states[-1]["created_at"]),
                ).fetchall()
                states.extend(dict(s) for s in later)
            payload = decode(intent.get("payload_json"))
            contract = payload.get("contract", {})
            fills = []
            if (
                ref
                and conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executions'"
                ).fetchone()
            ):
                fills = [
                    dict(f)
                    for f in conn.execute(
                        "SELECT * FROM executions WHERE order_ref=? ORDER BY execution_time, id",
                        (ref,),
                    )
                ]
            current = states[-1] if states else {}
            state = current.get("status") or ("unknown" if linked else "intent")
            filled = finite(current.get("filled"))
            qty = finite(order.get("quantity"))
            # Never synthesize Filled from incomplete or corrected execution reports.
            result.append(
                {
                    "symbol": order["symbol"],
                    "sec_type": order.get("sec_type"),
                    "action": order.get("action"),
                    "quantity": qty,
                    "limit_price": finite(order.get("limit_price")),
                    "currency": order.get("currency"),
                    "order_id": order_id,
                    "order_ref": ref,
                    "planned_at": utc(intent.get("created_at")) if is_intent else None,
                    "submitted_at": utc(order.get("created_at")) if linked else None,
                    "expiry": contract.get("lastTradeDateOrContractMonth"),
                    "strike": finite(contract.get("strike")),
                    "right": contract.get("right"),
                    "multiplier": finite(contract.get("multiplier")),
                    "status": state,
                    "status_label": ORDER_LABELS.get(state, state),
                    "filled": filled,
                    "remaining": finite(current.get("remaining")),
                    "partial": filled is not None
                    and qty is not None
                    and 0 < filled < qty,
                    "avg_fill_price": finite(current.get("avg_fill_price")),
                    "observed_at": utc(
                        current.get("created_at") or order.get("created_at")
                    ),
                    "timeline": [
                        {
                            "status": s["status"],
                            "label": ORDER_LABELS.get(s["status"], s["status"]),
                            "filled": s["filled"],
                            "remaining": s["remaining"],
                            "time": utc(s["created_at"]),
                        }
                        for s in states
                    ],
                    "executions": [
                        {
                            "time": utc(f["execution_time"]),
                            "quantity": f["shares"],
                            "price": f["price"],
                            "side": f["side"],
                        }
                        for f in fills
                    ],
                }
            )
        return result


def resolve_database(config_path: Path) -> Path:
    """Read v1/v2 database config without importing startup/migration/IBKR."""
    config = tomlkit.parse(config_path.read_text(encoding="utf-8")).unwrap()
    settings = config.get("runtime", config).get("database", {})
    if settings.get("enabled") is False:
        raise click.ClickException(
            "配置中的数据库未启用，请启用 runtime.database 后运行交易程序生成记录。"
        )
    url = settings.get("url")
    if url:
        if not url.startswith("sqlite:///") or url.endswith(":memory:"):
            raise click.ClickException("仪表盘需要本地 SQLite 文件。")
        return Path(url[len("sqlite:///") :]).expanduser().resolve()
    path = Path(settings.get("path", "data/thetagang.db")).expanduser()
    return (
        path if path.is_absolute() else config_path.resolve().parent / path
    ).resolve()


def make_handler(store: DashboardStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server: ThreadingHTTPServer

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def reply(
            self, code: int, body: bytes, content_type: str, download: bool = False
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            if download:
                self.send_header(
                    "Content-Disposition", 'attachment; filename="thetagang-run.json"'
                )
            self.end_headers()
            self.wfile.write(body)

        def json_reply(
            self, data: Any, code: int = 200, download: bool = False
        ) -> None:
            self.reply(
                code,
                json.dumps(clean(data), ensure_ascii=False, allow_nan=False).encode(),
                "application/json; charset=utf-8",
                download,
            )

        def do_GET(self) -> None:
            # Loopback bind + Host/Origin validation prevents remote site reads.
            allowed = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            if self.headers.get("Host") not in allowed:
                self.json_reply({"error": "仅允许从本机访问"}, 403)
                return
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{host}" for host in allowed}:
                self.json_reply({"error": "不允许跨站读取"}, 403)
                return
            url = urlsplit(self.path)
            assets = {
                "/": ("index.html", "text/html"),
                "/app.js": ("app.js", "text/javascript"),
                "/style.css": ("style.css", "text/css"),
                "/favicon.svg": ("favicon.svg", "image/svg+xml"),
            }
            if url.path in assets:
                name, mime = assets[url.path]
                self.reply(200, (ASSETS / name).read_bytes(), mime + "; charset=utf-8")
                return
            try:
                if url.path == "/api/runs":
                    args = parse_qs(url.query)
                    self.json_reply(
                        store.overview(
                            int(args.get("page", ["1"])[0]),
                            args.get("mode", ["all"])[0],
                        )
                    )
                elif match := re.fullmatch(r"/api/runs/(\d+)(/export)?", url.path):
                    self.json_reply(
                        store.detail(int(match[1])), download=bool(match[2])
                    )
                else:
                    self.json_reply({"error": "页面不存在"}, 404)
            except LookupError:
                self.json_reply({"error": "这次运行不存在"}, 404)
            except ValueError:
                self.json_reply({"error": "请求参数无效"}, 400)
            except sqlite3.Error:
                self.json_reply(
                    {
                        "error": "暂时无法读取执行记录，请检查数据库文件与表结构，或稍后刷新。"
                    },
                    503,
                )

    return Handler


@click.command()
@click.option(
    "--config",
    type=click.Path(path_type=Path, dir_okay=False),
    default="thetagang.toml",
    show_default=True,
)
@click.option(
    "--database",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="直接指定现有 SQLite 文件，优先于配置。",
)
@click.option(
    "--port", type=click.IntRange(1024, 65535), default=8765, show_default=True
)
def cli(config: Path, database: Path | None, port: int) -> None:
    """启动本地中文交易仪表盘；只读记录，不连接券商。"""
    try:
        path = database or resolve_database(config)
        store = DashboardStore(path)
        store.overview()
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise click.ClickException(f"无法启动仪表盘：{exc}") from exc
    click.echo(f"中文交易仪表盘：http://127.0.0.1:{port}")
    click.echo(f"只读数据库：{path} · 按 Ctrl+C 关闭")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    cli()
