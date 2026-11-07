"""job snoozed_until

Revision ID: b3f0c6a7d2e1
Revises: d8bcc9f1a16f
Create Date: 2026-10-17 20:05:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b3f0c6a7d2e1'
down_revision: str | None = 'd8bcc9f1a16f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.add_column(sa.Column('snoozed_until', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.drop_column('snoozed_until')
