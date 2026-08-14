from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_add_execution_account"
down_revision = "0003_add_execution_commission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("executions") as batch:
        batch.add_column(sa.Column("account", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("executions") as batch:
        batch.drop_column("account")
