"""real sending: profile switch, application claim time

Revision ID: f7b2c4d8e9a1
Revises: e6a1b3c9d742
Create Date: 2026-10-26 10:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f7b2c4d8e9a1'
down_revision: str | None = 'e6a1b3c9d742'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('profile', schema=None) as batch_op:
        batch_op.add_column(sa.Column('live_sending', sa.Boolean(), server_default='0',
                                      nullable=False))
    with op.batch_alter_table('application', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sending_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('application', schema=None) as batch_op:
        batch_op.drop_column('sending_at')
    with op.batch_alter_table('profile', schema=None) as batch_op:
        batch_op.drop_column('live_sending')
