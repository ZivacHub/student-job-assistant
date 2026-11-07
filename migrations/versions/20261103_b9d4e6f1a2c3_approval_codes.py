"""approval_code, approval_reply: approve an application by replying "DA" (plan 5.3b, D42)

Revision ID: b9d4e6f1a2c3
Revises: a8c3d5e7f912
Create Date: 2026-11-03 20:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b9d4e6f1a2c3'
down_revision: str | None = 'a8c3d5e7f912'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'approval_code',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('application_id', sa.Integer(), nullable=False),
        sa.Column('code_salt', sa.String(length=32), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('alert_message_id', sa.String(length=300), nullable=False),
        sa.Column('confirm_message_id', sa.String(length=300), nullable=True),
        sa.Column('draft_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('outcome', sa.String(length=20), nullable=True),
        sa.ForeignKeyConstraint(['application_id'], ['application.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_approval_code_application_id'), 'approval_code',
                    ['application_id'], unique=False)
    op.create_table(
        'approval_reply',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.String(length=300), nullable=False),
        sa.Column('code_id', sa.Integer(), nullable=True),
        sa.Column('outcome', sa.String(length=40), nullable=False),
        sa.Column('seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['code_id'], ['approval_code.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('message_id'),
    )


def downgrade() -> None:
    op.drop_table('approval_reply')
    op.drop_index(op.f('ix_approval_code_application_id'), table_name='approval_code')
    op.drop_table('approval_code')
