"""event accent color, image, description, and signup slots

Revision ID: c3d4e5f6a7b8
Revises: b1c2d3e4f5a6
Create Date: 2026-07-23 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('events', sa.Column('description', sa.Text(), nullable=True))
    op.add_column('events', sa.Column('image', sa.Text(), nullable=True))
    op.add_column('events', sa.Column('color', sa.Integer(), nullable=True))
    op.create_table(
        'event_slots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('capacity', sa.Integer(), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['event_id'], ['events.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column('attendance_records', sa.Column('slot_id', sa.Integer(), nullable=True))
    with op.batch_alter_table('attendance_records') as batch_op:
        batch_op.create_foreign_key(
            'fk_attendance_records_slot_id', 'event_slots', ['slot_id'], ['id']
        )


def downgrade() -> None:
    with op.batch_alter_table('attendance_records') as batch_op:
        batch_op.drop_constraint('fk_attendance_records_slot_id', type_='foreignkey')
    op.drop_column('attendance_records', 'slot_id')
    op.drop_table('event_slots')
    op.drop_column('events', 'color')
    op.drop_column('events', 'image')
    op.drop_column('events', 'description')
