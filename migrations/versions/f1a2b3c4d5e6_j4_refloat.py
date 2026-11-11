"""j4_refloat: envelope closing, refloat plans, supplement contributor, alert state

Revision ID: f1a2b3c4d5e6
Revises: e6f7a8b9c0d1
Create Date: 2026-11-10 22:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1a2b3c4d5e6'
down_revision = 'e6f7a8b9c0d1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('budget_month_category') as batch_op:
        batch_op.add_column(sa.Column('closed_at', sa.DateTime(), nullable=True))
    with op.batch_alter_table('operation') as batch_op:
        batch_op.add_column(sa.Column('contributor_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_operation_contributor_id', 'user', ['contributor_id'], ['id'])
    op.create_table(
        'refloat_plan',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('pot_id', sa.Integer(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('month', sa.Integer(), nullable=False),
        sa.Column('deficit', sa.Float(), nullable=False),
        sa.Column('months', sa.Integer(), nullable=False),
        sa.Column('split', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['pot_id'], ['pot.id'], name='fk_refloat_plan_pot_id'),
        sa.PrimaryKeyConstraint('id', name='pk_refloat_plan'),
        sa.UniqueConstraint('pot_id', 'year', 'month', name='uq_refloat_plan_pot_year_month'),
    )
    op.create_table(
        'refloat_cut',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['refloat_plan.id'], name='fk_refloat_cut_plan_id'),
        sa.ForeignKeyConstraint(['category_id'], ['category.id'], name='fk_refloat_cut_category_id',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_refloat_cut'),
        sa.UniqueConstraint('plan_id', 'category_id', name='uq_refloat_cut_plan_category'),
    )
    op.create_table(
        'pot_alert_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('pot_id', sa.Integer(), nullable=False),
        sa.Column('alert_type', sa.String(length=20), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['pot_id'], ['pot.id'], name='fk_pot_alert_state_pot_id'),
        sa.PrimaryKeyConstraint('id', name='pk_pot_alert_state'),
        sa.UniqueConstraint('pot_id', 'alert_type', name='uq_pot_alert_state'),
    )


def downgrade():
    op.drop_table('pot_alert_state')
    op.drop_table('refloat_cut')
    op.drop_table('refloat_plan')
    with op.batch_alter_table('operation') as batch_op:
        batch_op.drop_constraint('fk_operation_contributor_id', type_='foreignkey')
        batch_op.drop_column('contributor_id')
    with op.batch_alter_table('budget_month_category') as batch_op:
        batch_op.drop_column('closed_at')
