"""add candidacies table for persistent interview views

Revision ID: a1b2c3d4e5f6
Revises: c4561240bcd2
Create Date: 2026-07-01 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'c4561240bcd2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'candidacies',
        sa.Column('discord_id', sa.BigInteger(), nullable=False),
        sa.Column('callsign', sa.String(), nullable=False),
        sa.Column('thread_id', sa.BigInteger(), nullable=True),
        sa.Column('message_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('discord_id'),
    )


def downgrade() -> None:
    op.drop_table('candidacies')
