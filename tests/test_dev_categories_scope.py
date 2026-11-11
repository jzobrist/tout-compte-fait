import importlib.util
from pathlib import Path

from app.extensions import db as _db
from app.models import Category, Pot
from app.services import category_service

_SCRIPT = Path(__file__).parent.parent / 'scripts' / 'dev_categories_scope.py'


def _script():
    spec = importlib.util.spec_from_file_location('dev_categories_scope', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _shared(db, h, name, type_):
    a = Category(name=name, type=type_, pot_id=h.personal_a.id, scope='shared')
    db.session.add(a)
    db.session.flush()
    b = Category(name=name, type=type_, pot_id=h.personal_b.id, scope='shared', family_id=a.family_id)
    db.session.add(b)
    db.session.commit()
    return a, b


def _run(h):
    _script().run(h.user_a, _db, Category, Pot, category_service)


def test_script_renames_and_joins_whole_families(db, household_clean, capsys):
    h = household_clean
    twin = Category(name='Abonnements', type='fixed_auto', pot_id=h.common.id)
    db.session.add(twin)
    db.session.commit()
    abo = _shared(db, h, 'Abonnements perso', 'fixed_auto')
    loisirs = _shared(db, h, 'Loisirs perso', 'dynamic')
    _run(h)
    db.session.expire_all()
    for cat in abo:
        assert cat.name == 'Abonnements' and cat.family_id == twin.family_id and cat.scope == 'shared'
    a, b = loisirs
    assert a.name == b.name == 'Loisirs' and a.family_id == b.family_id
    first = capsys.readouterr().out
    assert 'JOIN' in first and 'RENAME' in first

    _run(h)  # idempotent
    second = capsys.readouterr().out
    assert 'JOIN' not in second and 'RENAME' not in second and 'SCOPE' not in second
    assert 'OK      « Abonnements » already in family' in second
