import datetime
import io

from app.extensions import db as _db
from app.models import BankAccount
from app.models.operation import Operation

CSV_HEADER = (
    'Date de comptabilisation;Libelle simplifie;Libelle operation;Reference;'
    'Informations complementaires;Type operation;Categorie;Sous categorie;'
    'Debit;Credit;Date operation;Date de valeur;Pointage operation\n'
)


def _row(ref, label='ACHAT', debit='-10,00'):
    d = '10/08/2026'
    return f'{d};{label};{label};{ref};;Carte;;;{debit};;{d};{d};0\n'


def _make(pot, name='Compte courant', **kw):
    acc = BankAccount(pot_id=pot.id, name=name, bank_name='Banque Populaire', **kw)
    _db.session.add(acc)
    _db.session.commit()
    return acc


def _import(client, account_id, content):
    return client.post(
        '/operations/import',
        data={'account_id': account_id,
              'csv_file': (io.BytesIO(content.encode()), 'r.csv')},
        content_type='multipart/form-data', follow_redirects=True)


def test_create_edit_archive_account(household_ab):
    h = household_ab
    resp = h.client_a.post('/accounts', data={
        'name': 'Compte courant', 'bank_name': 'Banque Populaire',
        'overdraft_limit': '800', 'pot_id': h.common.id}, follow_redirects=True)
    assert resp.status_code == 200
    acc = BankAccount.query.one()
    assert acc.pot_id == h.common.id and acc.overdraft_limit == 800
    assert 'Compte courant' in resp.get_data(as_text=True)

    h.client_a.post(f'/accounts/{acc.id}/edit', data={
        'name': 'Livret', 'bank_name': 'BP', 'overdraft_limit': '0'})
    _db.session.refresh(acc)
    assert acc.name == 'Livret' and acc.bank_name == 'BP' and acc.overdraft_limit == 0

    h.client_a.post(f'/accounts/{acc.id}/archive')
    _db.session.refresh(acc)
    assert acc.archived is True
    h.client_a.post(f'/accounts/{acc.id}/archive')
    _db.session.refresh(acc)
    assert acc.archived is False


def test_delete_account_without_operations(household_ab):
    h = household_ab
    acc = _make(h.common)
    h.client_a.post(f'/accounts/{acc.id}/delete')
    assert BankAccount.query.count() == 0


def test_delete_account_with_operations_refused(household_ab):
    h = household_ab
    acc = _make(h.common)
    _db.session.add(Operation(pot_id=h.common.id,
                              bank_account_id=acc.id, user_id=h.user_a.id, amount=5,
                              date=datetime.date.today()))
    _db.session.commit()
    resp = h.client_a.post(f'/accounts/{acc.id}/delete', follow_redirects=True)
    assert 'Ce compte a des opérations : archivez-le plutôt.' in resp.get_data(as_text=True)
    assert _db.session.get(BankAccount, acc.id) is not None


def test_b_cannot_see_or_edit_a_personal_account(household_ab):
    h = household_ab
    acc = _make(h.personal_a, name='Compte secret A')
    assert h.client_b.get(f'/accounts/{acc.id}/edit').status_code == 404
    assert h.client_b.post(f'/accounts/{acc.id}/edit', data={
        'name': 'x', 'bank_name': 'y', 'overdraft_limit': '0'}).status_code == 404
    assert h.client_b.post(f'/accounts/{acc.id}/archive').status_code == 404
    assert h.client_b.post(f'/accounts/{acc.id}/delete').status_code == 404
    _db.session.refresh(acc)
    assert acc.name == 'Compte secret A' and not acc.archived
    for pot in (h.common, h.personal_b):
        h.client_b.post(f'/pots/{pot.id}/select')
        assert 'Compte secret A' not in h.client_b.get('/accounts').get_data(as_text=True)
    # B cannot create into A's personal pot
    resp = h.client_b.post('/accounts', data={
        'name': 'Intrus', 'bank_name': 'X', 'overdraft_limit': '0', 'pot_id': h.personal_a.id})
    assert resp.status_code == 404


def test_import_into_account_sets_pot_and_account(household_ab):
    h = household_ab
    acc = _make(h.personal_a)
    _import(h.client_a, acc.id, CSV_HEADER + _row('R1'))
    op = Operation.query.filter_by(bank_reference='R1').one()
    assert op.pot_id == h.personal_a.id and op.bank_account_id == acc.id
    assert op.user_id == h.user_a.id


def test_same_reference_on_two_accounts_both_imported(household_ab):
    h = household_ab
    a1, a2 = _make(h.common, 'A1'), _make(h.common, 'A2')
    content = CSV_HEADER + _row('SAME')
    _import(h.client_a, a1.id, content)
    _import(h.client_a, a2.id, content)
    assert Operation.query.filter_by(bank_reference='SAME').count() == 2
    resp = _import(h.client_a, a1.id, content)
    assert Operation.query.filter_by(bank_reference='SAME').count() == 2
    assert 'doublon' in resp.get_data(as_text=True)


def test_dedup_ignores_other_members_personal_operations(household_ab):
    h = household_ab
    mine, theirs = _make(h.common, 'Commun'), _make(h.personal_a, 'Perso A')
    _import(h.client_a, theirs.id, CSV_HEADER + _row('LEAK'))
    resp = _import(h.client_b, mine.id, CSV_HEADER + _row('LEAK'))
    assert 'doublon' not in resp.get_data(as_text=True)
    assert Operation.query.filter_by(bank_reference='LEAK').count() == 2


def test_import_into_archived_account_refused(household_ab):
    h = household_ab
    acc = _make(h.common, archived=True)
    resp = _import(h.client_a, acc.id, CSV_HEADER + _row('R1'))
    assert Operation.query.count() == 0
    assert 'archiv' in resp.get_data(as_text=True)


def test_import_into_foreign_account_404(household_ab):
    h = household_ab
    acc = _make(h.personal_a)
    resp = _import(h.client_b, acc.id, CSV_HEADER + _row('R1'))
    assert resp.status_code == 404
    assert Operation.query.count() == 0


def test_import_page_without_account_shows_link(household_ab):
    html = household_ab.client_a.get('/operations/import').get_data(as_text=True)
    assert 'Aucun compte dans ce pot.' in html
    assert 'href="/accounts"' in html


def test_import_page_lists_only_active_accounts_of_current_pot(household_ab):
    h = household_ab
    _make(h.common, 'Visible'); _make(h.common, 'Archive', archived=True)
    _make(h.personal_a, 'AutrePot')
    html = h.client_a.get('/operations/import').get_data(as_text=True)
    assert 'Visible' in html and 'Archive' not in html and 'AutrePot' not in html


def test_owner_edit_page_renders_full_layout(household_ab):
    h = household_ab
    acc = _make(h.common, 'Mon compte')
    resp = h.client_a.get(f'/accounts/{acc.id}/edit')
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'bottom-nav' in html and 'value="Mon compte"' in html


def test_invalid_account_form_shows_errors(household_ab):
    h = household_ab
    resp = h.client_a.post('/accounts', data={
        'name': '', 'bank_name': 'BP', 'overdraft_limit': '-5', 'pot_id': h.common.id})
    html = resp.get_data(as_text=True)
    assert 'text-danger' in html
    assert BankAccount.query.count() == 0


def test_import_redirects_to_pointage_of_account_pot(household_ab):
    h = household_ab
    acc = _make(h.personal_a)
    with h.client_a.session_transaction() as sess:
        sess['current_pot_id'] = h.common.id
    resp = _import(h.client_a, acc.id, CSV_HEADER + _row('R9', label='VISIBLEOP'))
    assert 'VISIBLEOP' in resp.get_data(as_text=True)
    with h.client_a.session_transaction() as sess:
        assert sess['current_pot_id'] == h.personal_a.id
