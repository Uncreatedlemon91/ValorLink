"""add custom terminology overrides to guild_config

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-07-16 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, None] = 'c0d1e2f3a4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # JSON map of {key: override} applied on top of the chosen preset.
    op.add_column('guild_config', sa.Column('terminology_custom', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('guild_config', 'terminology_custom')
