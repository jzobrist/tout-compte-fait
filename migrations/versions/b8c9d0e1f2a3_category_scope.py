"""category_scope: family_id/scope on category, category_notice

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-11 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8c9d0e1f2a3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def _backfill(bind):
    pots = bind.execute(sa.text("SELECT id, household_id, kind FROM pot")).fetchall()
    cats = bind.execute(sa.text("SELECT id, pot_id, name, type FROM category ORDER BY id")).fetchall()
    pot_info = {p.id: p for p in pots}
    family = {}
    scope = {}
    for household_id in {p.household_id for p in pots}:
        personal_ids = [p.id for p in pots if p.household_id == household_id and p.kind == 'personal']
        common_ids = {p.id for p in pots if p.household_id == household_id and p.kind == 'common'}
        by_name = {}
        for c in cats:
            if c.pot_id in personal_ids:
                by_name.setdefault(c.name, []).append(c)
        groups = []  # (name, type, family_id)
        for name, group in by_name.items():
            pots_present = {c.pot_id for c in group}
            if personal_ids and pots_present == set(personal_ids) and len(group) >= len(personal_ids):
                fam = min(c.id for c in group)
                for c in group:
                    family[c.id] = fam
                    scope[c.id] = 'shared'
                groups.append((name, group[0].type, fam))
            else:
                for c in group:
                    family[c.id] = c.id
                    scope[c.id] = 'own'
                    groups.append((name, c.type, c.id))
        joined = set()
        for c in cats:
            if c.pot_id in common_ids:
                for name, type_, fam in groups:
                    if name == c.name and type_ == c.type and (name, type_, fam) not in joined:
                        family[c.id] = fam
                        joined.add((name, type_, fam))
                        break
    for c in cats:
        fam = family.get(c.id, c.id)
        bind.execute(sa.text("UPDATE category SET family_id=:f, scope=:s WHERE id=:i"),
                     {'f': fam, 's': scope.get(c.id), 'i': c.id})


def upgrade():
    op.create_table(
        'category_notice',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('message', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('seen_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_category_notice_user_id'),
        sa.PrimaryKeyConstraint('id', name='pk_category_notice'),
    )
    with op.batch_alter_table('category') as batch_op:
        batch_op.add_column(sa.Column('family_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('scope', sa.String(length=10), nullable=True))
    _backfill(op.get_bind())
    with op.batch_alter_table('category') as batch_op:
        batch_op.create_index('ix_category_family_id', ['family_id'])


def downgrade():
    with op.batch_alter_table('category') as batch_op:
        batch_op.drop_index('ix_category_family_id')
        batch_op.drop_column('scope')
        batch_op.drop_column('family_id')
    op.drop_table('category_notice')
