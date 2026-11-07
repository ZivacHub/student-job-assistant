"""timetable_source, timetable_board, timetable_departure: GPP Osijek's timetables (plan 4.11, D41)

Revision ID: c1e5f7a9b3d2
Revises: b9d4e6f1a2c3
Create Date: 2026-11-04 12:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c1e5f7a9b3d2'
down_revision: str | None = 'b9d4e6f1a2c3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'timetable_source',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('mode', sa.String(length=10), nullable=False),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_status', sa.String(length=20), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('valid_label', sa.String(length=60), nullable=True),
        sa.Column('content_hash', sa.String(length=64), nullable=True),
        sa.Column('boards', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('departures', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('mode'),
    )
    op.create_table(
        'timetable_board',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('mode', sa.String(length=10), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('route', sa.String(length=300), nullable=False),
        sa.Column('stop', sa.String(length=200), nullable=True),
        sa.Column('tolerance_min', sa.Integer(), nullable=True),
        sa.Column('pdf_url', sa.String(length=500), nullable=True),
        sa.Column('valid_label', sa.String(length=60), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_timetable_board_mode'), 'timetable_board', ['mode'], unique=False)
    op.create_table(
        'timetable_departure',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('board_id', sa.Integer(), nullable=False),
        sa.Column('day_type', sa.String(length=10), nullable=False),
        sa.Column('minute', sa.Integer(), nullable=False),
        sa.Column('suffix', sa.String(length=40), nullable=True),
        sa.ForeignKeyConstraint(['board_id'], ['timetable_board.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_departure_board_day', 'timetable_departure', ['board_id', 'day_type'],
                    unique=False)


def downgrade() -> None:
    op.drop_index('ix_departure_board_day', table_name='timetable_departure')
    op.drop_table('timetable_departure')
    op.drop_index(op.f('ix_timetable_board_mode'), table_name='timetable_board')
    op.drop_table('timetable_board')
    op.drop_table('timetable_source')
