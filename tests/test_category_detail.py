"""Détail d'une jauge : opérations comptées dans le dépensé, reclassement depuis la fenêtre."""
import datetime

from app.extensions import db as _db
from app.models import BankAccount, Category
from app.models.operation import Operation
from app.services.budget_service import spent_for_category, spent_operations, target_budget_month

HX = {'HX-Request': 'true'}
TODAY = datetime.date.today()


def _ym(pot):
    return target_budget_month(TODAY, pot.income_start_day or 1)


def _account(pot, name='Courant'):
    acc = BankAccount(pot_id=pot.id, name=name, bank_name='Banque Pop')
    _db.session.add(acc)
    _db.session.commit()
    return acc


def _cat(pot, name):
    cat = Category(name=name, type='fixed_amount', amount=100, pot_id=pot.id)
    _db.session.add(cat)
    _db.session.commit()
    return cat


def _movement(acc, user, ref, label, amount=10.0, day=None, direction='debit', classification=None,
              category=None):
    day = day or TODAY
    op = Operation(pot_id=category.pot_id if category else acc.pot_id, bank_account_id=acc.id,
                   user_id=user.id, amount=amount, date=day, label=label, bank_label=label,
                   bank_date=day, bank_reference=ref, direction=direction,
                   classification=classification, category_id=category.id if category else None)
    _db.session.add(op)
    _db.session.commit()
    return op


def _pending(pot, user, label, amount=10.0, day=None, category=None):
    op = Operation(pot_id=pot.id, user_id=user.id, amount=amount, entered_amount=amount,
                   date=day or TODAY, label=label, direction='debit', classification='category',
                   category_id=category.id if category else None)
    _db.session.add(op)
    _db.session.commit()
    return op


def _detail(client, cat, ym):
    return client.get(f'/category/{cat.id}/detail?year={ym[0]}&month={ym[1]}', headers=HX)


def test_spent_operations_match_spent_total(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Courses')
    other = _cat(h.common, 'Autre')
    _movement(acc, h.user_a, 'R1', 'CARREFOUR', 30.0, classification='category', category=cat)
    _movement(acc, h.user_a, 'R2', 'AVOIR', 5.0, direction='credit', classification='refund', category=cat)
    _pending(h.common, h.user_a, 'MARCHE', 12.0, category=cat)
    _movement(acc, h.user_a, 'R3', 'AILLEURS', 99.0, classification='category', category=other)
    _movement(acc, h.user_a, 'R4', 'VIEUX', 50.0, day=TODAY - datetime.timedelta(days=70),
              classification='category', category=cat)
    ym = _ym(h.common)
    ops = spent_operations(cat, *ym)
    assert sorted(o.label for o in ops) == ['AVOIR', 'CARREFOUR', 'MARCHE']
    signed = sum(-o.amount if o.direction == 'credit' else o.amount for o in ops)
    assert signed == spent_for_category(cat, *ym) == 37.0


def test_detail_lists_operations_and_total(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Courses')
    _movement(acc, h.user_a, 'R1', 'CARREFOUR', 30.0, classification='category', category=cat)
    _movement(acc, h.user_a, 'R2', 'AVOIR', 5.0, direction='credit', classification='refund', category=cat)
    _pending(h.common, h.user_a, 'MARCHE', 12.0, category=cat)
    resp = _detail(h.client_a, cat, _ym(h.common))
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'CARREFOUR' in html and 'AVOIR' in html and 'MARCHE' in html
    assert '37,00' in html
    assert 'en attente' in html


def test_dashboard_card_has_detail_button(household_ab):
    h = household_ab
    cat = _cat(h.common, 'Courses')
    html = h.client_a.get('/').get_data(as_text=True)
    assert f'/category/{cat.id}/detail' in html


def test_detail_of_partner_personal_category_is_404(household_ab):
    h = household_ab
    cat_b = _cat(h.personal_b, 'Secret B')
    assert _detail(h.client_a, cat_b, _ym(h.personal_b)).status_code == 404


def test_reclassify_bank_movement_from_detail(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Courses')
    other = _cat(h.common, 'Transports')
    mv = _movement(acc, h.user_a, 'R1', 'TOTAL ENERGIES', 40.0, classification='category', category=cat)
    ym = _ym(h.common)
    resp = h.client_a.post(f'/operations/{mv.id}/classify', headers=HX, data={
        'classification': 'category', 'category_id': other.id,
        'detail_cat': cat.id, 'year': ym[0], 'month': ym[1]})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'detail-modal-content' in html and 'TOTAL ENERGIES' not in html
    assert _db.session.get(Operation, mv.id).category_id == other.id


def test_detail_offers_off_budget_for_bank_movement(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Courses')
    _movement(acc, h.user_a, 'R1', 'CARREFOUR', 30.0, classification='category', category=cat)
    html = _detail(h.client_a, cat, _ym(h.common)).get_data(as_text=True)
    assert 'value="off_budget"' in html and 'value="internal"' in html


def test_recategorize_manual_entry_from_detail(household_ab):
    h = household_ab
    cat = _cat(h.common, 'Courses')
    other = _cat(h.common, 'Transports')
    entry = _pending(h.common, h.user_a, 'PARKING', 4.0, category=cat)
    ym = _ym(h.common)
    resp = h.client_a.post(f'/operations/{entry.id}/recategorize', headers=HX, data={
        'category_id': other.id, 'detail_cat': cat.id, 'year': ym[0], 'month': ym[1]})
    assert resp.status_code == 200
    assert 'PARKING' not in resp.get_data(as_text=True)
    assert _db.session.get(Operation, entry.id).category_id == other.id


def test_recategorize_rejects_bank_movement_and_foreign_category(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Courses')
    perso = _cat(h.personal_a, 'Perso A')
    mv = _movement(acc, h.user_a, 'R1', 'CB', 10.0, classification='category', category=cat)
    entry = _pending(h.common, h.user_a, 'ESPECES', 4.0, category=cat)
    assert h.client_a.post(f'/operations/{mv.id}/recategorize',
                           data={'category_id': cat.id}).status_code == 400
    assert h.client_a.post(f'/operations/{entry.id}/recategorize',
                           data={'category_id': perso.id}).status_code == 400
    cat_b = _cat(h.personal_b, 'Secret B')
    assert h.client_a.post(f'/operations/{entry.id}/recategorize',
                           data={'category_id': cat_b.id}).status_code == 404
    assert _db.session.get(Operation, entry.id).category_id == cat.id


def test_delete_manual_entry_from_detail(household_ab):
    h = household_ab
    cat = _cat(h.common, 'Courses')
    entry = _pending(h.common, h.user_a, 'BUS', 2.0, category=cat)
    ym = _ym(h.common)
    resp = h.client_a.post(f'/operations/{entry.id}/delete', headers=HX, data={
        'detail_cat': cat.id, 'year': ym[0], 'month': ym[1]})
    assert resp.status_code == 200
    assert 'detail-modal-content' in resp.get_data(as_text=True)
    assert _db.session.get(Operation, entry.id) is None


def test_detail_hides_partner_personal_account_name(household_ab):
    h = household_ab
    acc_b = _account(h.personal_b, name='CompteSecretB')
    common_cat = _cat(h.common, 'Courses')
    _movement(acc_b, h.user_b, 'R1', 'SUPERMARCHE', 20.0, classification='category', category=common_cat)
    html = _detail(h.client_a, common_cat, _ym(h.common)).get_data(as_text=True)
    assert 'SUPERMARCHE' in html
    assert 'CompteSecretB' not in html


def test_detail_title_and_menu_labelled(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Loisirs')
    _cat(h.personal_a, 'Loisirs')
    _movement(acc, h.user_a, 'R1', 'CINEMA', 30.0, classification='category', category=cat)
    html = _detail(h.client_a, cat, _ym(h.common)).get_data(as_text=True)
    assert 'Loisirs (Commun)' in html
    assert '>Loisirs (Commun)<' in html
