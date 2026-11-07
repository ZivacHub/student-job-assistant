"""ntfy phone push: profile switch

Revision ID: e6a1b3c9d742
Revises: c4d8e1f7a935
Create Date: 2026-10-25 12:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e6a1b3c9d742'
down_revision: str | None = 'c4d8e1f7a935'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('profile', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ntfy_enabled', sa.Boolean(), server_default='0',
                                      nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('profile', schema=None) as batch_op:
        batch_op.drop_column('ntfy_enabled')
