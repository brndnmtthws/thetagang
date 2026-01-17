from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_create_state_tables"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("config_path", sa.String(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("hostname", sa.String(), nullable=False),
        sa.Column("config_text", sa.Text(), nullable=True),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
    )
    op.create_table(
        "account_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "position_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("con_id", sa.Integer(), nullable=True),
        sa.Column("sec_type", sa.String(), nullable=True),
        sa.Column("position", sa.Float(), nullable=True),
        sa.Column("avg_cost", sa.Float(), nullable=True),
        sa.Column("market_price", sa.Float(), nullable=True),
        sa.Column("market_value", sa.Float(), nullable=True),
        sa.Column("unrealized_pnl", sa.Float(), nullable=True),
        sa.Column("realized_pnl", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(), nullable=True),
        sa.Column("exchange", sa.String(), nullable=True),
        sa.Column("multiplier", sa.String(), nullable=True),
        sa.Column("expiry", sa.String(), nullable=True),
        sa.Column("strike", sa.Float(), nullable=True),
        sa.Column("right", sa.String(), nullable=True),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("sec_type", sa.String(), nullable=True),
        sa.Column("con_id", sa.Integer(), nullable=True),
        sa.Column("exchange", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=True),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("limit_price", sa.Float(), nullable=True),
        sa.Column("order_type", sa.String(), nullable=True),
        sa.Column("order_ref", sa.String(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
    )
    op.create_table(
        "order_statuses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("filled", sa.Float(), nullable=True),
        sa.Column("remaining", sa.Float(), nullable=True),
        sa.Column("avg_fill_price", sa.Float(), nullable=True),
        sa.Column("last_fill_price", sa.Float(), nullable=True),
        sa.Column("perm_id", sa.Integer(), nullable=True),
    )
    op.create_table(
        "executions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("exec_id", sa.String(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("order_ref", sa.String(), nullable=True),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("side", sa.String(), nullable=True),
        sa.Column("shares", sa.Float(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("execution_time", sa.DateTime(), nullable=True),
        sa.Column("exchange", sa.String(), nullable=True),
        sa.UniqueConstraint("exec_id", name="uq_executions_exec_id"),
    )
    op.create_table(
        "historical_bars",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("bar_time", sa.DateTime(), nullable=False),
        sa.Column("timeframe", sa.String(), nullable=False),
        sa.Column("open", sa.Float(), nullable=True),
        sa.Column("high", sa.Float(), nullable=True),
        sa.Column("low", sa.Float(), nullable=True),
        sa.Column("close", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("bar_count", sa.Integer(), nullable=True),
        sa.Column("average", sa.Float(), nullable=True),
        sa.UniqueConstraint("symbol", "bar_time", "timeframe", name="uniq_bar_time"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("historical_bars")
    op.drop_table("executions")
    op.drop_table("order_statuses")
    op.drop_table("orders")
    op.drop_table("position_snapshots")
    op.drop_table("account_snapshots")
    op.drop_table("events")
    op.drop_table("runs")
