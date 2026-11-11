"""One-off: align the dev data with category families (scope/family_id).

Run after `flask db upgrade`. Idempotent; prints every change.

    venv/bin/python scripts/dev_categories_scope.py                    # real dev DB (instance/tcf_dev.db)
    venv/bin/python scripts/dev_categories_scope.py --database-url sqlite:////tmp/copy.db   # a copy
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

# (current personal name, new name)
RENAMES = [
    ('Abonnements perso', 'Abonnements'),
    ('Loisirs perso', 'Loisirs'),
    ('Restaurant (perso)', 'Restaurants / Cafés'),
]
DELETED = 'Vêtements perso'


def make_app(database_url):
    if not database_url:
        from app import create_app
        return create_app('development')
    # The production config reads its env at import time and fails fast without secrets (dummies are fine here),
    # so set them before importing the app.
    os.environ['DATABASE_URL'] = database_url
    os.environ.setdefault('SECRET_KEY', 'dev-script')
    os.environ.setdefault('BANK_SECRETS_KEY', '72bAoVYA7-JvULR4t9sgvLNNUQjSP_yTdCQ9HtHnnD8=')
    from app import create_app
    return create_app('production')


def run(user, db, Category, Pot, category_service):
    """Rename and join whole families (every member's exemplar), never a single exemplar."""
    pot = Pot.query.filter_by(kind='personal', owner_id=user.id).first()
    common = Pot.query.filter_by(kind='common', household_id=user.household_id).first()

    for old, new in RENAMES:
        cat = Category.query.filter_by(pot_id=pot.id, name=old).first()
        if cat is None:
            cat = Category.query.filter_by(pot_id=pot.id, name=new).first()
            if cat is None:
                print(f'SKIP    « {old} » not found (nor « {new} »)')
                continue
        elif Category.query.filter_by(pot_id=pot.id, name=new).first():
            print(f'SKIP    « {old} » not renamed: « {new} » already exists in the personal pot')
            continue

        members = category_service.family_members(cat)
        twin = Category.query.filter_by(pot_id=common.id, name=new).first()
        if twin is None:
            print(f'KEEP    « {new} » not joined: no common category with this exact name; stays its own family')
        elif twin.type != cat.type:
            print(f'KEEP    « {new} » not joined: type differs (common {twin.type}, personal {cat.type}); stays its own family')
        elif cat.family_id == twin.family_id:
            print(f'OK      « {new} » already in family {twin.family_id}')
        elif any(m.pot_id == common.id for m in members):
            print(f'KEEP    « {new} » not joined: family {cat.family_id} already has a common category')
        else:
            ids = [m.id for m in members]
            print(f'JOIN    family {cat.family_id} (categories {ids}) -> {twin.family_id} (common category {twin.id})')
            for member in members:
                member.family_id = twin.family_id
            db.session.commit()

        family = category_service.family_members(cat)
        stale = [m.id for m in family if m.name != new]
        if stale:
            print(f'RENAME  « {old} » -> « {new} » (family {cat.family_id}, categories {stale}) via rename_family')
            category_service.rename_family(user, cat, new)
        if cat.scope != 'shared':
            has_common = any(m.pot_id == common.id for m in family)
            print(f'SCOPE   « {new} » {cat.scope!r} -> \'shared\' via change_scope')
            category_service.change_scope(user, cat, has_common, 'shared')

    cat = Category.query.filter_by(pot_id=pot.id, name=DELETED).first()
    if cat is None:
        print(f'SKIP    « {DELETED} » already absent')
    else:
        print(f'DELETE  « {DELETED} » (category {cat.id}) via delete_family')
        category_service.delete_family(user, cat)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--database-url', help='target a DB other than the development one (e.g. a copy)')
    args = parser.parse_args()
    app = make_app(args.database_url)
    with app.app_context():
        from app.extensions import db
        from app.models import Category, Pot, User
        from app.services import category_service
        print('Target:', app.config['SQLALCHEMY_DATABASE_URI'], '->', db.engine.url)
        user = db.session.get(User, 1)
        run(user, db, Category, Pot, category_service)


if __name__ == '__main__':
    main()
