"""weekly officer digest settings on guild_config

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-07-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e8f9a0b1c2d3'
down_revision: Union[str, None] = 'd7e8f9a0b1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('guild_config', sa.Column('digest_channel_id', sa.BigInteger(), nullable=True))
    op.add_column('guild_config', sa.Column(
        'digest_enabled', sa.Boolean(), nullable=False, server_default='1'))
    op.add_column('guild_config', sa.Column('digest_last_sent_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('guild_config', 'digest_last_sent_at')
    op.drop_column('guild_config', 'digest_enabled')
    op.drop_column('guild_config', 'digest_channel_id')
