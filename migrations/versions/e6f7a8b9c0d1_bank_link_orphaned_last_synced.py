"""bank_link_orphaned_last_synced

Revision ID: e6f7a8b9c0d1
Revises: c4d2e5f6a7b8
Create Date: 2026-11-10 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e6f7a8b9c0d1'
down_revision = 'c4d2e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('bank_account_link') as batch_op:
        batch_op.add_column(sa.Column('orphaned', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('last_synced_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('bank_account_link') as batch_op:
        batch_op.drop_column('last_synced_at')
        batch_op.drop_column('orphaned')
