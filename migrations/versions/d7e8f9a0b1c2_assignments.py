"""secondary assignments

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-07-17 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, None] = 'c6d7e8f9a0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'assignments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('role_id', sa.BigInteger(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_leadership', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'member_assignments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('member_id', sa.BigInteger(), nullable=False),
        sa.Column('assignment_id', sa.Integer(), nullable=False),
        sa.Column('assigned_at', sa.DateTime(), nullable=True),
        sa.Column('assigned_by', sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(['assignment_id'], ['assignments.id']),
        sa.ForeignKeyConstraint(['member_id'], ['members.discord_id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('member_id', 'assignment_id', name='uq_member_assignment'),
    )


def downgrade() -> None:
    op.drop_table('member_assignments')
    op.drop_table('assignments')
