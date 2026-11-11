"""j6_contribution: constrained/provision/role flags, contribution plans, shares, allocations

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-11-11 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7b8c9d0e1f2'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('category') as batch_op:
        batch_op.add_column(sa.Column('constrained', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('provision', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('role', sa.String(length=20), nullable=True))
    op.create_table(
        'contribution_plan',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('pot_id', sa.Integer(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('month', sa.Integer(), nullable=False),
        sa.Column('position', sa.Float(), nullable=False),
        sa.Column('forced', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('validated_negative', sa.Boolean(), nullable=False),
        sa.Column('validated_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['pot_id'], ['pot.id'], name='fk_contribution_plan_pot_id'),
        sa.PrimaryKeyConstraint('id', name='pk_contribution_plan'),
        sa.UniqueConstraint('pot_id', 'year', 'month', name='uq_contribution_plan_pot_year_month'),
    )
    op.create_table(
        'contribution_validation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('snapshot', sa.String(length=500), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['contribution_plan.id'], name='fk_contribution_validation_plan_id'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_contribution_validation_user_id'),
        sa.PrimaryKeyConstraint('id', name='pk_contribution_validation'),
        sa.UniqueConstraint('plan_id', 'user_id', name='uq_contribution_validation'),
    )
    op.create_table(
        'contribution_share',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['contribution_plan.id'], name='fk_contribution_share_plan_id'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_contribution_share_user_id'),
        sa.PrimaryKeyConstraint('id', name='pk_contribution_share'),
        sa.UniqueConstraint('plan_id', 'user_id', name='uq_contribution_share'),
    )
    op.create_table(
        'provision_allocation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['contribution_plan.id'], name='fk_provision_allocation_plan_id'),
        sa.ForeignKeyConstraint(['category_id'], ['category.id'], name='fk_provision_allocation_category_id',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_provision_allocation'),
        sa.UniqueConstraint('plan_id', 'category_id', name='uq_provision_allocation'),
    )
    op.execute("UPDATE operation SET classification='contribution' WHERE classification='refloat_transfer'")
    op.execute(
        "UPDATE category SET role='common_transfer' "
        "WHERE name='Virement vers le commun' "
        "AND pot_id IN (SELECT id FROM pot WHERE kind='personal')"
    )


def downgrade():
    op.execute("UPDATE operation SET classification='refloat_transfer' WHERE classification='contribution'")
    op.drop_table('provision_allocation')
    op.drop_table('contribution_share')
    op.drop_table('contribution_validation')
    op.drop_table('contribution_plan')
    with op.batch_alter_table('category') as batch_op:
        batch_op.drop_column('role')
        batch_op.drop_column('provision')
        batch_op.drop_column('constrained')
