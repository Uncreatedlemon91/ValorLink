"""audit log table and member reminder opt-out

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-07-17 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5c6d7e8f9a0'
down_revision: Union[str, None] = 'a4b5c6d7e8f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'audit_entries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=True),
        sa.Column('actor_id', sa.BigInteger(), nullable=True),
        sa.Column('actor_name', sa.String(), nullable=True),
        sa.Column('source', sa.String(), nullable=False, server_default='web'),
        sa.Column('category', sa.String(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('target_id', sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_audit_entries_at', 'audit_entries', ['at'])
    op.add_column('members', sa.Column(
        'reminders_opt_out', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('members', 'reminders_opt_out')
    op.drop_index('ix_audit_entries_at', table_name='audit_entries')
    op.drop_table('audit_entries')
