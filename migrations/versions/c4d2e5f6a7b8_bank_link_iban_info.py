"""bank_link_iban_info

Revision ID: c4d2e5f6a7b8
Revises: b3a1c0de1234
Create Date: 2026-11-10 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d2e5f6a7b8'
down_revision = 'b3a1c0de1234'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('bank_account_link') as batch_op:
        batch_op.add_column(sa.Column('iban_fingerprint', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('iban_last4', sa.String(length=4), nullable=True))
        batch_op.add_column(sa.Column('label', sa.String(length=120), nullable=True))


def downgrade():
    with op.batch_alter_table('bank_account_link') as batch_op:
        batch_op.drop_column('label')
        batch_op.drop_column('iban_last4')
        batch_op.drop_column('iban_fingerprint')
