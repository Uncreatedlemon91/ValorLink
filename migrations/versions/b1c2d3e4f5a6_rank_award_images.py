"""insignia/medal images for ranks and awards

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-07-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = 'a0b1c2d3e4f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ranks', sa.Column('image', sa.Text(), nullable=True))
    op.add_column('award_types', sa.Column('image', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('award_types', 'image')
    op.drop_column('ranks', 'image')
