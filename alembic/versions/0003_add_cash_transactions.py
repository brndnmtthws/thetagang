from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_add_cash_transactions"
down_revision = "0002_add_order_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cash_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("unique_hash", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("section", sa.String(), nullable=True),
        sa.Column("row_type", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=True),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("trade_date", sa.DateTime(), nullable=True),
        sa.Column("settle_date", sa.DateTime(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("con_id", sa.Integer(), nullable=True),
        sa.Column("asset_category", sa.String(), nullable=True),
        sa.Column("transaction_type", sa.String(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.UniqueConstraint("source", "unique_hash", name="uniq_cash_transaction"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("cash_transactions")
