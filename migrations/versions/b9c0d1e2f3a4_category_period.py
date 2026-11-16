"""category.active_from / active_until (fixed charges limited in time)

Revision ID: b9c0d1e2f3a4
Revises: a1b2c3d4e5f6
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b9c0d1e2f3a4'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('category') as batch_op:
        batch_op.add_column(sa.Column('active_from', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('active_until', sa.Date(), nullable=True))


def downgrade():
    with op.batch_alter_table('category') as batch_op:
        batch_op.drop_column('active_until')
        batch_op.drop_column('active_from')
