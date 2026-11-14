"""envelope_ceilings: frozen monthly ceilings; drop refloat_cut

Manual refloat cuts are not migrated: envelopes fall back to the automatic prorata.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-11-14 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4a5b6c7d8e9'
down_revision = 'e3f4a5b6c7d8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'envelope_plan',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('pot_id', sa.Integer(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('month', sa.Integer(), nullable=False),
        sa.Column('validated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['pot_id'], ['pot.id'], name='fk_envelope_plan_pot_id'),
        sa.PrimaryKeyConstraint('id', name='pk_envelope_plan'),
        sa.UniqueConstraint('pot_id', 'year', 'month', name='uq_envelope_plan_pot_year_month'),
    )
    op.create_table(
        'envelope_ceiling',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Double(), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['envelope_plan.id'], name='fk_envelope_ceiling_plan_id'),
        sa.ForeignKeyConstraint(['category_id'], ['category.id'], name='fk_envelope_ceiling_category_id',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_envelope_ceiling'),
        sa.UniqueConstraint('plan_id', 'category_id', name='uq_envelope_ceiling_plan_category'),
    )
    op.drop_table('refloat_cut')


def downgrade():
    op.create_table(
        'refloat_cut',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Double(), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['refloat_plan.id'], name='fk_refloat_cut_plan_id'),
        sa.ForeignKeyConstraint(['category_id'], ['category.id'], name='fk_refloat_cut_category_id',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_refloat_cut'),
        sa.UniqueConstraint('plan_id', 'category_id', name='uq_refloat_cut_plan_category'),
    )
    op.drop_table('envelope_ceiling')
    op.drop_table('envelope_plan')
