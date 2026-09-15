from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_add_order_status_why_held"
down_revision = "0003_add_tail_hedge_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("order_statuses") as batch:
        batch.add_column(sa.Column("why_held", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("order_statuses") as batch:
        batch.drop_column("why_held")
