"""solidarity: household margin, contribution caps, plan.solidarity_adjusted

Revision ID: c1d2e3f4a5b6
Revises: b8c9d0e1f2a3
Create Date: 2026-11-11 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1d2e3f4a5b6'
down_revision = 'b8c9d0e1f2a3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('household', schema=None) as batch_op:
        batch_op.add_column(sa.Column('solidarity_margin', sa.Float(), nullable=False, server_default='0'))
    with op.batch_alter_table('contribution_plan', schema=None) as batch_op:
        batch_op.add_column(sa.Column('solidarity_adjusted', sa.Boolean(), nullable=False, server_default='0'))
    op.create_table(
        'contribution_cap',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('cap', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['plan_id'], ['contribution_plan.id'],
                                name='fk_contribution_cap_plan_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_contribution_cap_user_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('plan_id', 'user_id', name='uq_contribution_cap'),
    )


def downgrade():
    op.drop_table('contribution_cap')
    with op.batch_alter_table('contribution_plan', schema=None) as batch_op:
        batch_op.drop_column('solidarity_adjusted')
    with op.batch_alter_table('household', schema=None) as batch_op:
        batch_op.drop_column('solidarity_margin')
