"""add stage and notes to candidacies

Revision ID: d5e6f7a8b9c0
Revises: b2c3d4e5f6a7
Create Date: 2026-07-15 14:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default backfills existing rows; the app default keeps new rows set.
    op.add_column(
        'candidacies',
        sa.Column('stage', sa.String(), nullable=False, server_default='applied'),
    )
    op.add_column('candidacies', sa.Column('notes', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('candidacies', 'notes')
    op.drop_column('candidacies', 'stage')
