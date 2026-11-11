"""money_double_precision: FLOAT (single precision on MariaDB/MySQL) -> DOUBLE, values rounded

On MariaDB, sa.Float is a 4-byte FLOAT (~7 significant digits): 37.97 was stored as 37.970001...
This converts every money/ratio column to DOUBLE and removes the single-precision noise already
stored (money to the cent, ratios to 4 decimals). SQLite REAL is already double precision: there only the
declared type changes (batch table copy), so autogenerate stops reporting FLOAT -> Double.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-11-11

"""
from alembic import op
import sqlalchemy as sa


revision = 'e3f4a5b6c7d8'
down_revision = 'd2e3f4a5b6c7'
branch_labels = None
depends_on = None

# (table, column, nullable, server_default, decimals kept when cleaning stored values)
COLUMNS = [
    ('household', 'solidarity_margin', False, '0', 2),
    ('salary_forecast', 'amount', False, None, 2),
    ('bank_account', 'overdraft_limit', False, None, 2),
    ('bank_account', 'balance_booked', True, None, 2),
    ('bank_account', 'balance_value', True, None, 2),
    ('budget_month', 'total_income', False, None, 2),
    ('category', 'amount', True, None, 2),
    ('category', 'percentage', True, None, 4),
    ('category', 'min_amount', True, None, 2),
    ('category', 'max_amount', True, None, 2),
    ('contribution_plan', 'position', False, None, 2),
    ('refloat_plan', 'deficit', False, None, 2),
    ('refloat_plan', 'split', False, None, 4),
    ('budget_adjustment', 'amount', False, None, 2),
    ('budget_month_category', 'base_allocated', False, None, 2),
    ('budget_month_category', 'carry_in', False, None, 2),
    ('contribution_cap', 'cap', True, None, 2),
    ('contribution_share', 'amount', False, None, 2),
    ('operation', 'amount', False, None, 2),
    ('operation', 'entered_amount', True, None, 2),
    ('pending_bank_movement', 'amount', False, None, 2),
    ('provision_allocation', 'amount', False, None, 2),
    ('refloat_cut', 'amount', False, None, 2),
    ('income', 'amount', False, None, 2),
]


def _is_mysql():
    return op.get_bind().dialect.name in ('mysql', 'mariadb')


def _sqlite_retype(to_type, from_type):
    tables = {}
    for table, column, nullable, server_default, _ in COLUMNS:
        tables.setdefault(table, []).append((column, nullable, server_default))
    for table, columns in tables.items():
        with op.batch_alter_table(table) as batch_op:
            for column, nullable, server_default in columns:
                batch_op.alter_column(column, type_=to_type, existing_type=from_type,
                                      existing_nullable=nullable, existing_server_default=server_default)


def upgrade():
    if not _is_mysql():
        _sqlite_retype(sa.Double(), sa.Float())
        return
    for table, column, nullable, server_default, decimals in COLUMNS:
        # MODIFY COLUMN resets NULL-ness and default unless restated
        op.alter_column(table, column, type_=sa.Double(), existing_type=sa.Float(),
                        existing_nullable=nullable, existing_server_default=server_default)
        op.execute(f'UPDATE `{table}` SET `{column}` = ROUND(`{column}`, {decimals}) '
                   f'WHERE `{column}` IS NOT NULL')


def downgrade():
    if not _is_mysql():
        _sqlite_retype(sa.Float(), sa.Double())
        return
    for table, column, nullable, server_default, _ in COLUMNS:
        op.alter_column(table, column, type_=sa.Float(), existing_type=sa.Double(),
                        existing_nullable=nullable, existing_server_default=server_default)
