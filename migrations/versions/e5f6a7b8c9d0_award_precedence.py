"""order of precedence for the decorations rack

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('award_types',
                  sa.Column('position', sa.Integer(), nullable=False, server_default='0'))
    # Seed distinct positions from the existing catalogue order so the
    # reorder controls have something to move against straight away —
    # leaving every row at 0 would make the first "move up" a no-op.
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id FROM award_types ORDER BY name")).fetchall()
    for index, (award_id,) in enumerate(rows):
        conn.execute(
            sa.text("UPDATE award_types SET position = :p WHERE id = :id"),
            {"p": index, "id": award_id},
        )


def downgrade() -> None:
    op.drop_column('award_types', 'position')
