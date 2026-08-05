"""unit documents (markdown handbooks/SOPs)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'unit_documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('admin_only', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('created_by_name', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_by', sa.BigInteger(), nullable=True),
        sa.Column('updated_by_name', sa.String(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )


def downgrade() -> None:
    op.drop_table('unit_documents')
