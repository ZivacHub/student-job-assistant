"""gmail setup: profile gmail address, alert switch, phone test result

Revision ID: c4d8e1f7a935
Revises: a7c91e45d2b8
Create Date: 2026-10-22 10:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c4d8e1f7a935'
down_revision: str | None = 'a7c91e45d2b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('profile', schema=None) as batch_op:
        batch_op.add_column(sa.Column('gmail_address', sa.String(length=254), nullable=True))
        batch_op.add_column(sa.Column('gmail_checked_at', sa.DateTime(timezone=True),
                                      nullable=True))
        batch_op.add_column(sa.Column('gmail_problem', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('alerts_enabled', sa.Boolean(), server_default='0',
                                      nullable=False))
        batch_op.add_column(sa.Column('alerts_enabled_at', sa.DateTime(timezone=True),
                                      nullable=True))
        batch_op.add_column(sa.Column('phone_push_result', sa.String(length=3), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('profile', schema=None) as batch_op:
        batch_op.drop_column('phone_push_result')
        batch_op.drop_column('alerts_enabled_at')
        batch_op.drop_column('alerts_enabled')
        batch_op.drop_column('gmail_problem')
        batch_op.drop_column('gmail_checked_at')
        batch_op.drop_column('gmail_address')
