"""salary_forecast: salaries typed by a member for the plan simulation

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-11-11 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd2e3f4a5b6c7'
down_revision = 'c1d2e3f4a5b6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'salary_forecast',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('member_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_salary_forecast_user_id',
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['member_id'], ['user.id'], name='fk_salary_forecast_member_id',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'member_id', name='uq_salary_forecast'),
    )


def downgrade():
    op.drop_table('salary_forecast')
