from datetime import date

from app.extensions import db as _db
from app.models import BankAccount, Operation, PendingBankMovement
from app.services.account_service import move_account
from tests.test_bank_connect import _connect, _link, _setup, _FormParser, fake  # noqa: F401
from tests.test_bank_sync import make_account, make_conn, make_link


def _op(h, acc, pot, ref, classification=None, label='SUPERMARCHE 0001 FR 750001PARIS',
        bank_label='240101 CB****1234 | SUPERMARCHE 0001 | FR 750001PARIS'):
    op = Operation(pot_id=pot.id, bank_account_id=acc.id, user_id=h.user_a.id, amount=5.0,
                   date=date(2026, 11, 9), label=label, bank_label=bank_label,
                   bank_date=date(2026, 11, 9), bank_reference=ref, direction='debit',
                   classification=classification)
    _db.session.add(op)
    _db.session.commit()
    return op


# 1. sync button on « Ma banque »
def test_bank_page_sync_button_by_status(household_ab, fake):
    h = household_ab
    for status, expected in (('active', True), ('error', True), ('expired', False), ('setup', False)):
        conn = make_conn(h.user_a, f'app-{status}', status=status)
        html = h.client_a.get('/bank').get_data(as_text=True)
        assert ('Synchroniser maintenant' in html) is expected, status
        assert ('action="/bank/sync"' in html) is expected, status
        _db.session.delete(conn)
        _db.session.commit()


# 2. pointage main text = reconstructed label
def test_pointage_main_text_is_label_with_bank_label_below(household_ab):
    h = household_ab
    acc = make_account(h.common)
    _op(h, acc, h.common, 'R1')
    html = h.client_a.get('/operations/pointage').get_data(as_text=True)
    assert 'fw-semibold text-truncate">SUPERMARCHE 0001 FR 750001PARIS<' in html
    assert '240101 CB****1234 | SUPERMARCHE 0001 | FR 750001PARIS' in html


def test_pointage_no_bank_label_line_when_same(household_ab):
    h = household_ab
    acc = make_account(h.common)
    _op(h, acc, h.common, 'R1', label='SAME', bank_label='SAME')
    html = h.client_a.get('/operations/pointage').get_data(as_text=True)
    assert html.count('SAME') == 1


# 3. mapping form
def test_map_form_has_disabled_placeholder_selected(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    html = h.client_a.get('/bank').get_data(as_text=True)
    assert '<option value="" disabled selected>— choisir un pot —</option>' in html


def test_map_requires_a_choice(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    link = _link('uid-perso-a')
    r = h.client_a.post(f'/bank/links/{link.id}/map', data={'pot_id': '', 'name': 'X'},
                        follow_redirects=True)
    assert 'Choisissez un pot ou un compte.' in r.get_data(as_text=True)
    assert BankAccount.query.count() == 0
    assert _link('uid-perso-a').bank_account_id is None and not _link('uid-perso-a').ignored


# 4. move account
def test_move_account_to_classify_ops_follow(household_ab):
    h = household_ab
    acc = make_account(h.common)
    todo = _op(h, acc, h.common, 'R1')
    done = _op(h, acc, h.common, 'R2', classification='off_budget')
    _db.session.add(PendingBankMovement(bank_account_id=acc.id, date=date.today(), amount=1,
                                        direction='debit', label='p'))
    _db.session.commit()
    r = h.client_a.post(f'/accounts/{acc.id}/edit', data={
        'name': 'Courant', 'bank_name': 'Banque', 'overdraft_limit': '0',
        'pot_id': h.personal_a.id})
    assert r.status_code == 302
    _db.session.expire_all()
    assert _db.session.get(BankAccount, acc.id).pot_id == h.personal_a.id
    assert _db.session.get(Operation, todo.id).pot_id == h.personal_a.id
    assert _db.session.get(Operation, done.id).pot_id == h.common.id
    assert PendingBankMovement.query.one().bank_account_id == acc.id


def test_b_cannot_move_a_personal_account(household_ab):
    h = household_ab
    acc = make_account(h.personal_a)
    r = h.client_b.post(f'/accounts/{acc.id}/edit', data={
        'name': 'x', 'bank_name': 'y', 'overdraft_limit': '0', 'pot_id': h.personal_b.id})
    assert r.status_code == 404
    assert _db.session.get(BankAccount, acc.id).pot_id == h.personal_a.id


def test_b_cannot_move_common_account_into_a_personal_pot(household_ab):
    h = household_ab
    acc = make_account(h.common)
    r = h.client_b.post(f'/accounts/{acc.id}/edit', data={
        'name': 'x', 'bank_name': 'y', 'overdraft_limit': '0', 'pot_id': h.personal_a.id})
    assert r.status_code == 404
    assert _db.session.get(BankAccount, acc.id).pot_id == h.common.id


def test_edit_form_offers_pot_choice(household_ab):
    h = household_ab
    acc = make_account(h.common)
    html = h.client_a.get(f'/accounts/{acc.id}/edit').get_data(as_text=True)
    assert f'<option value="{h.personal_a.id}"' in html


# 5. brand
def test_brand_is_tout_compte_fait(household_ab):
    html = household_ab.client_a.get('/').get_data(as_text=True)
    assert 'Tout Compte Fait</a>' in html and 'Compta<' not in html


def test_cannot_move_joint_account_linked_by_other_member_to_personal(household_ab):
    h = household_ab
    acc = make_account(h.common)
    make_link(make_conn(h.user_b, 'app-b'), acc, 'uid-b')
    data = {'name': 'Renamed', 'bank_name': 'Banque', 'overdraft_limit': '0',
            'pot_id': h.personal_a.id}
    r = h.client_a.post(f'/accounts/{acc.id}/edit', data=data, follow_redirects=True)
    assert 'il doit rester dans le pot commun' in r.get_data(as_text=True)
    _db.session.expire_all()
    a = _db.session.get(BankAccount, acc.id)
    assert a.pot_id == h.common.id and a.name != 'Renamed'


def test_can_move_when_only_own_or_no_foreign_link(household_ab):
    h = household_ab
    acc = make_account(h.common)
    make_link(make_conn(h.user_a, 'app-a'), acc, 'uid-a')
    r = h.client_a.post(f'/accounts/{acc.id}/edit', data={
        'name': 'Courant', 'bank_name': 'Banque', 'overdraft_limit': '0',
        'pot_id': h.personal_a.id})
    assert r.status_code == 302
    _db.session.expire_all()
    assert _db.session.get(BankAccount, acc.id).pot_id == h.personal_a.id
