from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_add_tail_hedge_entries"
down_revision = "0002_add_order_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("executions") as batch:
        batch.add_column(sa.Column("account", sa.String(), nullable=True))

    op.create_table(
        "tail_hedge_entries",
        sa.Column("account", sa.String(), primary_key=True),
        sa.Column("entry_id", sa.String(), primary_key=True),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("con_id", sa.Integer(), nullable=False),
        sa.Column("expiration", sa.String(), nullable=False),
        sa.Column("strike", sa.Float(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("entry_limit_price", sa.Float(), nullable=False),
        sa.Column("entered_at", sa.DateTime(), nullable=False),
        sa.Column("estimated_cost", sa.Float(), nullable=False),
        sa.Column("recovered_cost", sa.Float(), nullable=False),
        sa.Column("pending_recovery_quantity", sa.Integer(), nullable=True),
        sa.Column("pending_recovery_per_contract", sa.Float(), nullable=True),
        sa.Column("pending_recovery_enqueued_at", sa.DateTime(), nullable=True),
        sa.Column("pending_recovery_initial_quantity", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("tail_hedge_entries")
    with op.batch_alter_table("executions") as batch:
        batch.drop_column("account")
