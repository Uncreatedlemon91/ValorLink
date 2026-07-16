"""recruitment questions + candidacy answers

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-07-16 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a8b9c0d1e2f3'
down_revision: Union[str, None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'recruitment_questions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('prompt', sa.String(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('required', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column('candidacies', sa.Column('answers', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('candidacies', 'answers')
    op.drop_table('recruitment_questions')
