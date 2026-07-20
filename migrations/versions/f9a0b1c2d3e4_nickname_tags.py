"""unit tag on guild_config and tag on companies

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-07-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f9a0b1c2d3e4'
down_revision: Union[str, None] = 'e8f9a0b1c2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('guild_config', sa.Column('unit_tag', sa.String(), nullable=True))
    op.add_column('companies', sa.Column('tag', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('companies', 'tag')
    op.drop_column('guild_config', 'unit_tag')
