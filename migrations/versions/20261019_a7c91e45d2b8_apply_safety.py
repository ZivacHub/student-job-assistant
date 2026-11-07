"""apply safety: profile sending numbers and kill switch, application approved_hash

Revision ID: a7c91e45d2b8
Revises: b3f0c6a7d2e1
Create Date: 2026-10-19 12:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a7c91e45d2b8'
down_revision: str | None = 'b3f0c6a7d2e1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('profile', schema=None) as batch_op:
        for name, kind, default in (
            ('apply_daily_cap', sa.Integer(), '10'),
            ('apply_cooldown_days', sa.Integer(), '14'),
            ('apply_undo_minutes', sa.Integer(), '2'),
            ('sending_paused', sa.Boolean(), '0'),
        ):
            batch_op.add_column(sa.Column(name, kind, server_default=default, nullable=False))
    with op.batch_alter_table('application', schema=None) as batch_op:
        batch_op.add_column(sa.Column('approved_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('dry_run_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('application', schema=None) as batch_op:
        batch_op.drop_column('dry_run_at')
        batch_op.drop_column('approved_hash')
    with op.batch_alter_table('profile', schema=None) as batch_op:
        batch_op.drop_column('sending_paused')
        batch_op.drop_column('apply_undo_minutes')
        batch_op.drop_column('apply_cooldown_days')
        batch_op.drop_column('apply_daily_cap')
