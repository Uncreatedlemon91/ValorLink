"""member profile, LOA self-request, and event after-action fields

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-07-16 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, None] = 'e6f7a8b9c0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Member self-service profile
    op.add_column('members', sa.Column('timezone', sa.String(), nullable=True))
    op.add_column('members', sa.Column('ingame_name', sa.String(), nullable=True))
    op.add_column('members', sa.Column('availability', sa.String(), nullable=True))
    op.add_column('members', sa.Column('bio', sa.Text(), nullable=True))
    # Member-initiated leave request awaiting an officer's decision
    op.add_column('members', sa.Column('loa_requested_until', sa.DateTime(), nullable=True))
    op.add_column('members', sa.Column('loa_reason', sa.String(), nullable=True))
    # Event after-action record
    op.add_column('events', sa.Column('outcome', sa.String(), nullable=True))
    op.add_column('events', sa.Column('after_action', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('events', 'after_action')
    op.drop_column('events', 'outcome')
    op.drop_column('members', 'loa_reason')
    op.drop_column('members', 'loa_requested_until')
    op.drop_column('members', 'bio')
    op.drop_column('members', 'availability')
    op.drop_column('members', 'ingame_name')
    op.drop_column('members', 'timezone')
