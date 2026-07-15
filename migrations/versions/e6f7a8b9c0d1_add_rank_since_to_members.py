"""add rank_since to members

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-07-15 15:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('members', sa.Column('rank_since', sa.DateTime(), nullable=True))
    # Backfill: treat existing members as holding their rank since they enrolled.
    op.execute("UPDATE members SET rank_since = joined_date WHERE rank_since IS NULL")


def downgrade() -> None:
    op.drop_column('members', 'rank_since')
