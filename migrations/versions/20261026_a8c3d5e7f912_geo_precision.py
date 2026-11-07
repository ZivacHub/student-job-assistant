"""job.geo_precision: is the job's spot an exact place or only the town centre

Revision ID: a8c3d5e7f912
Revises: f7b2c4d8e9a1
Create Date: 2026-10-26 20:30:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a8c3d5e7f912'
down_revision: str | None = 'f7b2c4d8e9a1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # empty (NULL) = not checked yet: the app fills it in for old ads (app/geo/recheck.py)
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.add_column(sa.Column('geo_precision', sa.String(length=10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.drop_column('geo_precision')
