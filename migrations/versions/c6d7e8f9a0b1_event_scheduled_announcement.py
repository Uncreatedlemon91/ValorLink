"""event scheduled announcement (lead time)

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-07-17 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c6d7e8f9a0b1'
down_revision: Union[str, None] = 'b5c6d7e8f9a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('events', sa.Column('announce_lead_minutes', sa.Integer(), nullable=True))
    op.add_column('events', sa.Column(
        'announced', sa.Boolean(), nullable=False, server_default='0'))
    # Every existing event was announced immediately on creation, so mark them
    # announced — the scheduler must never re-post history.
    op.execute("UPDATE events SET announced = 1")


def downgrade() -> None:
    op.drop_column('events', 'announced')
    op.drop_column('events', 'announce_lead_minutes')
