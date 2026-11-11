from datetime import date

from app.models import Category, CategoryNotice, Operation
from tests.test_category_delete import _account, _bank_op, _cat, _pending, _shared


def test_nav_has_no_categories_link(household_ab):
    h = household_ab
    html = h.client_a.get('/').get_data(as_text=True)
    assert 'nav-link" href="/categories"' not in html
    assert '🏷️' not in html
    assert 'href="/categories"' in h.client_a.get('/settings').get_data(as_text=True)


def test_list_shows_badges_once_per_family(db, household_ab):
    h = household_ab
    _cat(db, h.common, 'Zumba')
    a, _ = _shared(db, h, 'Zumba')
    for c in Category.query.filter_by(name='Zumba'):
        c.family_id = a.family_id
    db.session.commit()
    html = h.client_a.get('/categories').get_data(as_text=True)
    assert html.count('>Zumba') == 1
    assert '>Commun</span>' in html and '>Perso</span>' in html


def test_create_via_form(db, household_ab):
    h = household_ab
    resp = h.client_a.post('/categories', data={
        'name': 'Voyages', 'type': 'fixed_amount', 'common': 'y', 'personal': 'own',
        'common-amount': '40', 'personal-amount': '15'})
    assert resp.status_code == 302
    rows = {c.pot_id: c for c in Category.query.filter_by(name='Voyages')}
    assert set(rows) == {h.common.id, h.personal_a.id}
    assert rows[h.common.id].amount == 40 and rows[h.personal_a.id].amount == 15
    assert rows[h.personal_a.id].scope == 'own'


def test_create_without_place_is_refused(household_ab):
    resp = household_ab.client_a.post('/categories', data={
        'name': 'Nulle', 'type': 'fixed_amount', 'personal': ''})
    assert resp.status_code == 400
    assert 'Choisissez au moins un emplacement.' in resp.get_data(as_text=True)
    assert Category.query.filter_by(name='Nulle').count() == 0


def test_delete_confirmation_shows_accessible_count(db, household_ab):
    h = household_ab
    a, b = _shared(db, h)
    acc_a, acc_b = _account(db, h.personal_a), _account(db, h.personal_b)
    for i in range(2):
        _bank_op(db, h.user_a, a, acc_a, f'A{i}')
    for i in range(3):
        _bank_op(db, h.user_b, b, acc_b, f'B{i}')
    html = h.client_a.get('/categories').get_data(as_text=True)
    assert 'data-impact="2"' in html and 'data-impact="3"' not in html


def test_edit_changes_name_type_scope_and_settings(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common, 'Cadeaux')
    resp = h.client_a.post(f'/categories/{cat.id}/edit', data={
        'name': 'Fêtes', 'type': 'dynamic', 'common': 'y', 'personal': 'shared',
        'common-percentage': '0.05', 'common-amount': '99'})
    assert resp.status_code == 302
    rows = Category.query.filter_by(name='Fêtes').all()
    assert {r.pot_id for r in rows} == {h.common.id, h.personal_a.id, h.personal_b.id}
    common = next(r for r in rows if r.pot_id == h.common.id)
    assert common.type == 'dynamic' and common.percentage == 0.05 and common.amount is None


def test_edit_name_conflict_is_flashed(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common, 'Cadeaux')
    _cat(db, h.common, 'Fêtes')
    resp = h.client_a.post(f'/categories/{cat.id}/edit', data={
        'name': 'fêtes', 'type': 'fixed_amount', 'common': 'y'})
    assert resp.status_code == 400
    assert 'Une catégorie porte déjà ce nom.' in resp.get_data(as_text=True)


def test_edit_scope_removal_exposes_impact(db, household_ab):
    h = household_ab
    cat = _cat(db, h.personal_a, 'Solo', scope='own')
    acc = _account(db, h.personal_a)
    _bank_op(db, h.user_a, cat, acc, 'S1')
    html = h.client_a.get(f'/categories/{cat.id}/edit').get_data(as_text=True)
    assert 'data-impacts=' in html and '&#34;to_reclassify&#34;: 1' in html.replace('"', '&#34;')


def test_partner_cannot_touch_own_category(db, household_ab):
    h = household_ab
    cat = _cat(db, h.personal_a, 'Secret B', scope='own')
    for method, url in (('get', f'/categories/{cat.id}/edit'), ('post', f'/categories/{cat.id}/edit'),
                        ('post', f'/categories/{cat.id}/delete')):
        assert getattr(h.client_b, method)(url).status_code == 404
    assert db.session.get(Category, cat.id) is not None


def test_notice_banner_shown_once(db, household_ab):
    h = household_ab
    db.session.add(CategoryNotice(user_id=h.user_b.id, message='La catégorie Sport a été supprimée par A — 2 opérations à reclasser.'))
    db.session.commit()
    assert 'category-notice' not in h.client_a.get('/').get_data(as_text=True)
    html = h.client_b.get('/').get_data(as_text=True)
    assert 'La catégorie Sport a été supprimée par A' in html
    assert 'href="/operations/pointage"' in html.split('category-notice')[1]
    assert 'La catégorie Sport' not in h.client_b.get('/').get_data(as_text=True)


def test_notice_not_consumed_by_fragments(db, household_ab):
    h = household_ab
    db.session.add(CategoryNotice(user_id=h.user_b.id, message='Msg unique'))
    db.session.commit()
    h.client_b.get('/operations/form', headers={'HX-Request': 'true'})
    assert 'Msg unique' in h.client_b.get('/').get_data(as_text=True)


def test_edit_is_atomic_when_a_added_pot_clashes(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common, 'Cadeaux')
    _cat(db, h.personal_a, 'Fêtes', scope='own')
    resp = h.client_a.post(f'/categories/{cat.id}/edit', data={
        'name': 'Fêtes', 'type': 'fixed_amount', 'common': 'y', 'personal': 'own'})
    assert resp.status_code == 400
    assert 'Une catégorie porte déjà ce nom.' in resp.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Category, cat.id).name == 'Cadeaux'
    assert Category.query.filter_by(name='Fêtes').count() == 1


def test_update_family_service_rolls_back_on_clash(db, household_ab):
    import pytest
    from app.services import category_service
    from app.services.category_service import CategoryError
    h = household_ab
    cat = _cat(db, h.common, 'Cadeaux')
    _cat(db, h.personal_a, 'Fêtes', scope='own')
    with pytest.raises(CategoryError):
        category_service.update_family(h.user_a, cat, 'Fêtes', 'dynamic', True, 'own', {})
    db.session.expire_all()
    assert db.session.get(Category, cat.id).name == 'Cadeaux'
    assert db.session.get(Category, cat.id).type == 'fixed_amount'


def _common_plus_hidden_own(db, h):
    """A creates « Sport » Commun + Juste pour moi, with a bank movement and a pending entry on A's own."""
    from app.services import category_service
    cats = category_service.create_category(h.user_a, 'Sport', 'fixed_amount', True, 'own',
                                            {'common': {'amount': 50}, 'personal': {'amount': 30}})
    common = next(c for c in cats if c.pot_id == h.common.id)
    own_a = next(c for c in cats if c.pot_id == h.personal_a.id)
    acc = _account(db, h.personal_a)
    bank = _bank_op(db, h.user_a, own_a, acc, 'H1')
    pend = _pending(db, h.user_a, own_a)
    return common, own_a.id, bank.id, pend.id


def _assert_own_a_untouched(db, h, own_id, bank_id, pend_id, scope='own'):
    db.session.expire_all()
    own = db.session.get(Category, own_id)
    assert own is not None and own.name == 'Sport' and own.amount == 30 and own.scope == scope
    assert own.pot_id == h.personal_a.id
    assert db.session.get(Operation, bank_id).category_id == own_id
    assert db.session.get(Operation, pend_id) is not None
    assert CategoryNotice.query.filter_by(user_id=h.user_a.id).count() == 0
    return own


def test_partner_edit_of_common_keeps_hidden_own(db, household_clean):
    h = household_clean
    common, own_id, bank_id, pend_id = _common_plus_hidden_own(db, h)
    html = h.client_b.get(f'/categories/{common.id}/edit').get_data(as_text=True)
    assert 'selected value="own"' not in html and 'selected value=""' in html
    resp = h.client_b.post(f'/categories/{common.id}/edit', data={
        'name': 'Sport', 'type': 'fixed_amount', 'common': 'y', 'personal': '',
        'common-amount': '60', 'common-carry_over': 'y'})
    assert resp.status_code == 302
    own = _assert_own_a_untouched(db, h, own_id, bank_id, pend_id)
    assert db.session.get(Category, common.id).amount == 60
    assert own.family_id != common.family_id


def test_partner_own_choice_keeps_hidden_own(db, household_clean):
    h = household_clean
    common, own_id, bank_id, pend_id = _common_plus_hidden_own(db, h)
    resp = h.client_b.post(f'/categories/{common.id}/edit', data={
        'name': 'Sport', 'type': 'fixed_amount', 'common': 'y', 'personal': 'own'})
    assert resp.status_code == 302
    _assert_own_a_untouched(db, h, own_id, bank_id, pend_id)
    family = Category.query.filter_by(family_id=common.family_id).all()
    assert {(c.pot_id, c.scope) for c in family} == {(h.common.id, None), (h.personal_b.id, 'own')}


def test_partner_shared_choice_absorbs_hidden_own(db, household_clean):
    h = household_clean
    common, own_id, bank_id, pend_id = _common_plus_hidden_own(db, h)
    resp = h.client_b.post(f'/categories/{common.id}/edit', data={
        'name': 'Sport', 'type': 'fixed_amount', 'common': 'y', 'personal': 'shared'})
    assert resp.status_code == 302
    own = _assert_own_a_untouched(db, h, own_id, bank_id, pend_id, scope='shared')
    assert own.family_id == common.family_id
    family = Category.query.filter_by(family_id=common.family_id).all()
    assert {(c.pot_id, c.scope) for c in family} == {
        (h.common.id, None), (h.personal_a.id, 'shared'), (h.personal_b.id, 'shared')}


def test_partner_delete_of_common_keeps_hidden_own(db, household_clean):
    h = household_clean
    common, own_id, bank_id, pend_id = _common_plus_hidden_own(db, h)
    assert h.client_b.post(f'/categories/{common.id}/delete').status_code == 302
    assert db.session.get(Category, common.id) is None
    _assert_own_a_untouched(db, h, own_id, bank_id, pend_id)


def test_partner_rename_of_common_keeps_hidden_own_name(db, household_clean):
    h = household_clean
    common, own_id, bank_id, pend_id = _common_plus_hidden_own(db, h)
    resp = h.client_b.post(f'/categories/{common.id}/edit', data={
        'name': 'Sports', 'type': 'fixed_amount', 'common': 'y', 'personal': ''})
    assert resp.status_code == 302
    _assert_own_a_untouched(db, h, own_id, bank_id, pend_id)
    assert db.session.get(Category, common.id).name == 'Sports'


def test_partner_type_change_keeps_hidden_own(db, household_clean):
    h = household_clean
    common, own_id, bank_id, pend_id = _common_plus_hidden_own(db, h)
    resp = h.client_b.post(f'/categories/{common.id}/edit', data={
        'name': 'Sport', 'type': 'dynamic', 'common': 'y', 'personal': '', 'common-percentage': '0.1'})
    assert resp.status_code == 302
    own = _assert_own_a_untouched(db, h, own_id, bank_id, pend_id)
    assert own.type == 'fixed_amount'


def test_partner_cannot_convert_own_family(db, household_clean):
    h = household_clean
    _, own_id, _, _ = _common_plus_hidden_own(db, h)
    for method, url, data in (('get', f'/categories/{own_id}/edit', None),
                              ('post', f'/categories/{own_id}/edit',
                               {'name': 'Sport', 'type': 'fixed_amount', 'personal': 'shared'})):
        assert getattr(h.client_b, method)(url, data=data).status_code == 404
    assert db.session.get(Category, own_id).scope == 'own'


def test_locked_family_settings_stay_editable(db, household_ab):
    h = household_ab
    cat = Category.query.filter_by(pot_id=h.personal_a.id, role='common_transfer').one()
    resp = h.client_a.post(f'/categories/{cat.id}/edit', data={
        'name': cat.name, 'type': cat.type, 'personal': 'shared',
        'personal-amount': '123', 'personal-carry_over': 'y'})
    assert resp.status_code == 302
    db.session.expire_all()
    assert db.session.get(Category, cat.id).amount == 123
    for change in ({'name': 'Autre'}, {'type': 'dynamic'}, {'personal': 'own'}, {'common': 'y'}):
        data = {'name': cat.name, 'type': cat.type, 'personal': 'shared', **change}
        resp = h.client_a.post(f'/categories/{cat.id}/edit', data=data)
        assert resp.status_code == 400
        assert 'Cette catégorie ne peut pas être modifiée.' in resp.get_data(as_text=True)
    h.client_a.post(f'/categories/{cat.id}/delete')
    db.session.expire_all()
    cat = db.session.get(Category, cat.id)
    assert cat is not None and cat.name == 'Virement vers le commun' and cat.scope == 'shared'
