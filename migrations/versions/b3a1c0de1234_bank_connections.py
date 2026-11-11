"""bank_connections

Revision ID: b3a1c0de1234
Revises: cfd9da34503d
Create Date: 2026-11-10 16:59:44.470678

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b3a1c0de1234'
down_revision = 'cfd9da34503d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('bank_connection',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('app_id', sa.String(length=64), nullable=False),
    sa.Column('private_key_enc', sa.LargeBinary(), nullable=False),
    sa.Column('aspsp_name', sa.String(length=120), nullable=False),
    sa.Column('session_id_enc', sa.LargeBinary(), nullable=True),
    sa.Column('valid_until', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='setup', nullable=False),
    sa.Column('auth_state', sa.String(length=64), nullable=True),
    sa.Column('last_sync_at', sa.DateTime(), nullable=True),
    sa.Column('last_error', sa.String(length=255), nullable=True),
    sa.Column('expiry_notified_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_bank_connection_user_id'),
    sa.PrimaryKeyConstraint('id', name='pk_bank_connection'),
    sa.UniqueConstraint('user_id', name='uq_bank_connection_user_id')
    )
    op.create_table('bank_account_link',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('connection_id', sa.Integer(), nullable=False),
    sa.Column('bank_account_id', sa.Integer(), nullable=True),
    sa.Column('provider_uid', sa.String(length=100), nullable=False),
    sa.Column('is_primary', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('ignored', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.ForeignKeyConstraint(['bank_account_id'], ['bank_account.id'], name='fk_bank_link_bank_account_id'),
    sa.ForeignKeyConstraint(['connection_id'], ['bank_connection.id'], name='fk_bank_link_connection_id'),
    sa.PrimaryKeyConstraint('id', name='pk_bank_account_link'),
    sa.UniqueConstraint('connection_id', 'provider_uid', name='uq_bank_link_connection_uid')
    )
    op.create_table('pending_bank_movement',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bank_account_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('amount', sa.Float(), nullable=False),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('label', sa.String(length=200), nullable=True),
    sa.ForeignKeyConstraint(['bank_account_id'], ['bank_account.id'], name='fk_pending_movement_bank_account_id'),
    sa.PrimaryKeyConstraint('id', name='pk_pending_bank_movement')
    )
    with op.batch_alter_table('bank_account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('iban_fingerprint', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('balance_booked', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('balance_value', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('balance_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_bank_account_iban_fingerprint'), ['iban_fingerprint'], unique=False)



def downgrade():
    with op.batch_alter_table('bank_account', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bank_account_iban_fingerprint'))
        batch_op.drop_column('balance_at')
        batch_op.drop_column('balance_value')
        batch_op.drop_column('balance_booked')
        batch_op.drop_column('iban_fingerprint')

    op.drop_table('pending_bank_movement')
    op.drop_table('bank_account_link')
    op.drop_table('bank_connection')
