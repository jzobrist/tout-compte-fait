import importlib.util
from pathlib import Path

from app.models import Category, CategoryNotice, Household, Pot, User

_MIGRATION = Path(__file__).parent.parent / 'migrations' / 'versions' / 'b8c9d0e1f2a3_category_scope.py'


def _backfill(db):
    spec = importlib.util.spec_from_file_location('mig_category_scope', _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    db.session.execute(db.text('UPDATE category SET family_id=NULL, scope=NULL'))
    db.session.commit()
    mod._backfill(db.session.connection())
    db.session.commit()
    db.session.expire_all()


def _add(db, pot, name, type_='fixed_amount'):
    cat = Category(name=name, type=type_, pot_id=pot.id)
    db.session.add(cat)
    db.session.flush()
    return cat


def test_new_category_gets_its_own_family(db, household_ab):
    cat = Category(name='X', type='fixed_amount', pot_id=household_ab.common.id)
    db.session.add(cat)
    db.session.flush()
    assert cat.family_id == cat.id
    db.session.commit()
    assert cat.family_id == cat.id
    assert cat.scope is None


def test_explicit_family_kept(db, household_ab):
    first = Category(name='X', type='fixed_amount', pot_id=household_ab.common.id)
    db.session.add(first)
    db.session.commit()
    second = Category(name='X', type='fixed_amount', pot_id=household_ab.personal_a.id,
                      family_id=first.family_id)
    db.session.add(second)
    db.session.commit()
    assert second.family_id == first.family_id != second.id


def test_category_notice_roundtrip(db, household_ab):
    notice = CategoryNotice(user_id=household_ab.user_a.id, message='m')
    db.session.add(notice)
    db.session.commit()
    assert notice.seen_at is None
    assert notice.created_at is not None


def test_backfill_two_members(db, household_clean):
    h = household_clean
    both_a = _add(db, h.personal_a, 'Sport')
    both_b = _add(db, h.personal_b, 'Sport')
    only_a = _add(db, h.personal_a, 'Solo')
    common_same = _add(db, h.common, 'Sport')
    common_other_type = _add(db, h.common, 'Sport', 'dynamic')
    common_alone = _add(db, h.common, 'Autre')
    db.session.commit()
    _backfill(db)
    assert both_a.family_id == both_b.family_id == min(both_a.id, both_b.id)
    assert both_a.scope == both_b.scope == 'shared'
    assert only_a.scope == 'own' and only_a.family_id == only_a.id
    assert common_same.family_id == both_a.family_id and common_same.scope is None
    assert common_other_type.family_id == common_other_type.id
    assert common_alone.family_id == common_alone.id


def test_backfill_one_member_household_all_shared(db, household_clean):
    h = Household(name='Solo')
    db.session.add(h)
    db.session.flush()
    user = User(email='s@test.com', display_name='S', password_hash='x', household_id=h.id)
    db.session.add(user)
    db.session.flush()
    personal = Pot(household_id=h.id, kind='personal', owner_id=user.id, name='Perso')
    common = Pot(household_id=h.id, kind='common', name='Commun')
    db.session.add_all([personal, common])
    db.session.flush()
    p1, p2 = _add(db, personal, 'A'), _add(db, personal, 'B')
    c1 = _add(db, common, 'A')
    db.session.commit()
    _backfill(db)
    assert (p1.scope, p2.scope) == ('shared', 'shared')
    assert p1.family_id == p1.id and p2.family_id == p2.id
    assert c1.family_id == p1.id


def test_personal_seeds_are_shared_without_vetements(db, household_ab):
    h = household_ab
    a = Category.query.filter_by(pot_id=h.personal_a.id).all()
    b = Category.query.filter_by(pot_id=h.personal_b.id).all()
    assert 'Vêtements perso' not in {c.name for c in a}
    assert a and all(c.scope == 'shared' for c in a + b)
    for name in ('Loisirs perso', 'Virement vers le commun'):
        ca = next(c for c in a if c.name == name)
        cb = next(c for c in b if c.name == name)
        assert ca.family_id == cb.family_id
    assert all(c.role == 'common_transfer' for c in a + b if c.name == 'Virement vers le commun')


def test_joiner_gets_shared_families_only(app, db):
    from app.services.household_service import create_household, create_personal_pot
    user_a = User(email='ja@test.com', display_name='JA', password_hash='x')
    household = create_household('Foyer J', user_a)
    pot_a = Pot.query.filter_by(owner_id=user_a.id).one()
    shared = _add(db, pot_a, 'Sport', 'dynamic')
    shared.scope = 'shared'
    shared.percentage = 0.2
    own = _add(db, pot_a, 'Secret')
    own.scope = 'own'
    db.session.commit()
    user_b = User(email='jb@test.com', display_name='JB', password_hash='x', household_id=household.id)
    db.session.add(user_b)
    db.session.flush()
    pot_b = create_personal_pot(user_b)
    names = {c.name: c for c in Category.query.filter_by(pot_id=pot_b.id)}
    assert names['Sport'].family_id == shared.family_id
    assert names['Sport'].scope == 'shared' and names['Sport'].amount is None
    assert names['Sport'].percentage is None
    assert 'Secret' not in names


def test_common_seeds_have_no_scope(db, household_ab):
    cats = Category.query.filter_by(pot_id=household_ab.common.id).all()
    assert cats and all(c.scope is None for c in cats)


def test_joiner_absorbs_same_name_category(app, db):
    from app.services import category_service
    from app.services.household_service import create_household, create_personal_pot
    user_a = User(email='ka@test.com', display_name='KA', password_hash='x')
    household = create_household('Foyer K', user_a)
    pot_a = Pot.query.filter_by(owner_id=user_a.id).one()
    user_b = User(email='kb@test.com', display_name='KB', password_hash='x', household_id=household.id)
    db.session.add(user_b)
    db.session.flush()
    pot_b = Pot(household_id=household.id, kind='personal', owner_id=user_b.id, name='Perso B')
    db.session.add(pot_b)
    db.session.flush()
    mine = _add(db, pot_b, 'sport')
    shared = _add(db, pot_a, 'Sport', 'dynamic')
    shared.scope = 'shared'
    db.session.commit()
    category_service.attach_shared_families(pot_b)
    db.session.commit()
    rows = Category.query.filter(Category.pot_id == pot_b.id, db.func.lower(Category.name) == 'sport').all()
    assert rows == [mine]
    assert mine.family_id == shared.family_id and mine.scope == 'shared' and mine.type == 'dynamic'


def _names(db, pot):
    return [c.name.lower() for c in Category.query.filter_by(pot_id=pot.id)]


def test_create_common_and_shared(db, household_clean):
    from app.services import category_service
    h = household_clean
    rows = category_service.create_category(
        h.user_a, 'Sport', 'fixed_amount', True, 'shared',
        {'common': {'amount': 30}, 'personal': {'amount': 10}})
    assert len(rows) == 3 and len({c.family_id for c in rows}) == 1
    by_pot = {c.pot_id: c for c in rows}
    assert by_pot[h.common.id].amount == 30
    assert by_pot[h.personal_a.id].amount == 10
    assert by_pot[h.personal_b.id].amount is None


def test_create_own_only_in_author_pot(db, household_clean):
    from app.services import category_service
    h = household_clean
    rows = category_service.create_category(h.user_a, 'Secret', 'dynamic', False, 'own', {})
    assert len(rows) == 1 and rows[0].pot_id == h.personal_a.id and rows[0].scope == 'own'
    assert not _names(db, h.personal_b)


def test_create_requires_a_place(db, household_clean):
    import pytest
    from app.services import category_service
    with pytest.raises(category_service.CategoryError):
        category_service.create_category(household_clean.user_a, 'X', 'dynamic', False, None, {})


def test_name_conflict_in_accessible_pot_refused(db, household_ab):
    import pytest
    from app.services import category_service
    with pytest.raises(category_service.CategoryError, match='Une catégorie porte déjà ce nom.'):
        category_service.create_category(household_ab.user_a, 'transports', 'dynamic', True, None, {})


def test_shared_create_absorbs_partner_own_category(db, household_clean):
    from app.models import Operation
    from app.services import category_service
    h = household_clean
    theirs = _add(db, h.personal_b, 'Sport')
    theirs.scope = 'own'
    theirs.amount = 20
    db.session.add(Operation(pot_id=h.personal_b.id, user_id=h.user_b.id, category_id=theirs.id, amount=5, date=__import__('datetime').date(2026, 6, 1)))
    db.session.commit()
    rows = category_service.create_category(h.user_a, 'Sport', 'dynamic', False, 'shared', {})
    assert len(rows) == 2
    db.session.refresh(theirs)
    assert theirs.family_id == rows[0].family_id and theirs.scope == 'shared'
    assert theirs.type == 'dynamic' and theirs.amount is None
    assert theirs.operations.count() == 1
    assert _names(db, h.personal_b) == ['sport']


def test_rename_applies_to_all_exemplars(db, household_clean):
    from app.services import category_service
    h = household_clean
    rows = category_service.create_category(h.user_a, 'Sport', 'dynamic', True, 'shared', {})
    category_service.rename_family(h.user_a, rows[0], 'Fitness')
    assert {c.name for c in category_service.family_members(rows[0])} == {'Fitness'}
    assert len(category_service.family_members(rows[0])) == 3


def test_rename_locked_family_refused(db, household_ab):
    import pytest
    from app.services import category_service
    cat = Category.query.filter_by(pot_id=household_ab.personal_a.id, name='Virement vers le commun').one()
    with pytest.raises(category_service.CategoryError, match='ne peut pas être modifiée'):
        category_service.rename_family(household_ab.user_a, cat, 'Autre')


def test_families_for_hides_partner_own(db, household_clean):
    from app.services import category_service
    h = household_clean
    category_service.create_category(h.user_b, 'Cadeau', 'dynamic', False, 'own', {})
    category_service.create_category(h.user_a, 'Sport', 'dynamic', False, 'shared', {})
    fams = category_service.families_for(h.user_a)
    assert 'Cadeau' not in {f['name'] for f in fams}
    sport = next(f for f in fams if f['name'] == 'Sport')
    assert sport['badges'] == ['Perso']
    assert [c.pot_id for c in sport['exemplars']] == [h.personal_a.id]


def test_display_name(db, household_ab):
    from app.services import category_service
    common = Category.query.filter_by(pot_id=household_ab.common.id).first()
    perso = Category.query.filter_by(pot_id=household_ab.personal_a.id).first()
    assert category_service.display_name(common).endswith(' (Commun)')
    assert category_service.display_name(perso).endswith(' (Perso)')


def _fam(db, h, **kw):
    from app.services import category_service
    return category_service.create_category(h.user_a, 'Sport', 'fixed_amount', **kw)


def test_add_common_to_personal_family(db, household_clean):
    from app.services import category_service
    h = household_clean
    rows = _fam(db, h, common=False, personal='shared', settings={})
    category_service.change_scope(h.user_a, rows[0], True, 'shared')
    pots = {c.pot_id for c in category_service.family_members(rows[0])}
    assert pots == {h.common.id, h.personal_a.id, h.personal_b.id}


def test_remove_common_uses_safe_delete(db, household_clean):
    from datetime import date
    from app.models import BankAccount, Operation
    from app.services import category_service
    h = household_clean
    rows = _fam(db, h, common=True, personal='shared', settings={})
    common = next(c for c in rows if c.pot_id == h.common.id)
    acc = BankAccount(pot_id=h.common.id, name='C', bank_name='B')
    db.session.add(acc)
    db.session.flush()
    op = Operation(pot_id=h.common.id, user_id=h.user_a.id, amount=10.0, date=date(2026, 11, 3),
                   label='x', direction='debit', bank_account_id=acc.id, bank_reference='R1',
                   bank_label='x', bank_date=date(2026, 11, 3), category_id=common.id,
                   classification='category')
    db.session.add(op)
    db.session.commit()
    impact = category_service.scope_change_impact(h.user_a, rows[0], False, 'shared')
    assert impact == {'to_reclassify': 1, 'pending_deleted': 0}
    category_service.change_scope(h.user_a, rows[0], False, 'shared')
    db.session.refresh(op)
    assert op.category_id is None and op.pot_id == h.common.id
    assert {c.pot_id for c in category_service.family_members(rows[1])} == {h.personal_a.id, h.personal_b.id}


def test_shared_to_own_keeps_actor_exemplar(db, household_clean):
    from app.services import category_service
    h = household_clean
    rows = _fam(db, h, common=False, personal='shared', settings={})
    mine = next(c for c in rows if c.pot_id == h.personal_b.id)
    other_id = next(c.id for c in rows if c.pot_id == h.personal_a.id)
    category_service.change_scope(h.user_b, mine, False, 'own')
    members = category_service.family_members(mine)
    assert [m.id for m in members] == [mine.id] and mine.scope == 'own'
    assert db.session.get(Category, other_id) is None


def test_own_to_shared_reaches_partner(db, household_clean):
    from app.services import category_service
    h = household_clean
    rows = _fam(db, h, common=False, personal='own', settings={})
    category_service.change_scope(h.user_a, rows[0], False, 'shared')
    assert {c.pot_id for c in category_service.family_members(rows[0])} == {h.personal_a.id, h.personal_b.id}


def test_cannot_remove_last_place(db, household_clean):
    import pytest
    from app.services import category_service
    h = household_clean
    rows = _fam(db, h, common=True, personal=None, settings={})
    with pytest.raises(category_service.CategoryError, match='Choisissez au moins un emplacement.'):
        category_service.change_scope(h.user_a, rows[0], False, None)


def test_change_type_clears_incompatible(db, household_clean):
    from app.services import category_service
    h = household_clean
    rows = _fam(db, h, common=True, personal='shared',
                settings={'personal': {'amount': 10, 'constrained': True}, 'common': {'amount': 30}})
    category_service.change_type(h.user_a, rows[0], 'dynamic')
    for c in category_service.family_members(rows[0]):
        assert c.type == 'dynamic' and c.amount is None and c.constrained is False
        c.percentage, c.min_amount, c.max_amount = 0.1, 1, 5
    db.session.commit()
    category_service.change_type(h.user_a, rows[0], 'fixed_amount')
    for c in category_service.family_members(rows[0]):
        assert c.type == 'fixed_amount'
        assert c.percentage is None and c.min_amount is None and c.max_amount is None


def test_locked_family_scope_refused(db, household_ab):
    import pytest
    from app.services import category_service
    cat = Category.query.filter_by(pot_id=household_ab.personal_a.id, name='Virement vers le commun').one()
    with pytest.raises(category_service.CategoryError):
        category_service.change_scope(household_ab.user_a, cat, True, 'shared')
    with pytest.raises(category_service.CategoryError):
        category_service.change_type(household_ab.user_a, cat, 'dynamic')


def test_null_family_is_a_single_row_family(db, household_clean):
    from app.services import category_service
    h = household_clean
    rows = [Category(name=n, type='fixed_amount', pot_id=h.personal_a.id, scope='own') for n in ('X', 'Y')]
    db.session.add_all(rows)
    db.session.commit()
    db.session.execute(db.text('UPDATE category SET family_id=NULL'))
    db.session.commit()
    db.session.expire_all()
    x, y = rows
    assert x.family_id is None and y.family_id is None
    assert category_service.family_members(x) == [x]
    assert [f['exemplars'] for f in category_service.families_for(h.user_a)] == [[x], [y]]
    category_service.rename_family(h.user_a, x, 'Z')
    assert y.name == 'Y'
    category_service.delete_family(h.user_a, x)
    assert db.session.get(Category, y.id) is not None
    assert Category.query.count() == 1
