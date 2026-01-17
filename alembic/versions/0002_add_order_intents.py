from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_add_order_intents"
down_revision = "0001_create_state_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_intents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
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
        sa.Column("tif", sa.String(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
    )

    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("intent_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_orders_intent_id", "order_intents", ["intent_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint("fk_orders_intent_id", type_="foreignkey")
        batch.drop_column("intent_id")
    op.drop_table("order_intents")
