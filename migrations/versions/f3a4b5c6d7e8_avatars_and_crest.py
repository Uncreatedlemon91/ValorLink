"""member avatar hash and unit crest

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-07-16 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, None] = 'e2f3a4b5c6d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('members', sa.Column('avatar', sa.String(), nullable=True))
    op.add_column('guild_config', sa.Column('crest', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('guild_config', 'crest')
    op.drop_column('members', 'avatar')
