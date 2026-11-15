"""category.paid_by_user_id + contribution_share.advance

Revision ID: a1b2c3d4e5f6
Revises: f4a5b6c7d8e9
Create Date: 2026-11-15
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'f4a5b6c7d8e9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('category') as batch_op:
        batch_op.add_column(sa.Column('paid_by_user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_category_paid_by_user_id', 'user', ['paid_by_user_id'], ['id'],
                                    ondelete='SET NULL')
    with op.batch_alter_table('contribution_share') as batch_op:
        batch_op.add_column(sa.Column('advance', sa.Double(), nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('contribution_share') as batch_op:
        batch_op.drop_column('advance')
    with op.batch_alter_table('category') as batch_op:
        batch_op.drop_constraint('fk_category_paid_by_user_id', type_='foreignkey')
        batch_op.drop_column('paid_by_user_id')
