from datetime import date, datetime, timedelta, timezone

import pytest

from app.extensions import db as _db
from app.models import (BankAccount, BankAccountLink, BankConnection, Operation,
                        PendingBankMovement, User)
from app.services import bank_sync_service, email_service, secrets_service
from app.services.enablebanking_client import EnableBankingAuthError, EnableBankingError
from tests.test_bank_connect import _pem


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def book(ref, amount, d, indicator='DBIT', lines=('CARTE SUPERMARCHE',)):
    return {'status': 'BOOK', 'entry_reference': ref, 'booking_date': d.isoformat(),
            'transaction_amount': {'amount': str(amount), 'currency': 'EUR'},
            'credit_debit_indicator': indicator, 'remittance_information': list(lines)}


def pdng(amount, d, lines=('PAIEMENT EN COURS',)):
    return {'status': 'PDNG', 'booking_date': d.isoformat(),
            'transaction_amount': {'amount': str(amount), 'currency': 'EUR'},
            'credit_debit_indicator': 'DBIT', 'remittance_information': list(lines)}


class FakeSyncClient:
    def __init__(self):
        self.txs = {}        # uid -> list
        self.bals = {}       # uid -> list
        self.calls = []
        self.error = None

    def transactions(self, uid, date_from):
        self.calls.append((uid, date_from))
        if self.error:
            raise self.error
        return list(self.txs.get(uid, []))

    def balances(self, uid):
        return list(self.bals.get(uid, []))


@pytest.fixture
def fake(monkeypatch):
    from collections import defaultdict
    clients = defaultdict(FakeSyncClient)

    def get_client(connection):
        return clients[connection.app_id]

    monkeypatch.setattr(bank_sync_service, 'get_client', get_client)
    return clients


def make_conn(user, app_id, status='active', last_sync=None, valid_days=90):
    conn = BankConnection(user_id=user.id, app_id=app_id, aspsp_name='Banque', status=status,
                          private_key_enc=secrets_service.encrypt('x'),
                          session_id_enc=secrets_service.encrypt('s'),
                          last_sync_at=last_sync, valid_until=utcnow() + timedelta(days=valid_days))
    _db.session.add(conn)
    _db.session.commit()
    return conn


def make_account(pot, name='Courant'):
    acc = BankAccount(pot_id=pot.id, name=name, bank_name='Banque')
    _db.session.add(acc)
    _db.session.commit()
    return acc


def make_link(conn, acc, uid, primary=True):
    link = BankAccountLink(connection_id=conn.id, bank_account_id=acc.id, provider_uid=uid,
                           is_primary=primary)
    _db.session.add(link)
    _db.session.commit()
    return link


@pytest.fixture
def setup_a(household_ab, fake):
    h = household_ab
    acc = make_account(h.common)
    conn = make_conn(h.user_a, 'app-a')
    make_link(conn, acc, 'uid-1')
    return h, conn, acc, fake['app-a']


def test_sync_imports_book_only_with_labels_and_reference(setup_a):
    h, conn, acc, cl = setup_a
    today = date.today()
    cl.txs['uid-1'] = [
        book('R1', '12.50', today, lines=['240101 CB****1234', 'SUPERMARCHE  LYON']),
        book('R2', '1000', today, 'CRDT', lines=['SALAIRE']),
        pdng('5', today),
    ]
    res = bank_sync_service.sync_connection(conn)
    assert (res.imported, res.pending, res.errors) == (2, 1, [])
    ops = {o.bank_reference: o for o in Operation.query.all()}
    assert set(ops) == {'R1', 'R2'}
    assert ops['R1'].label == 'SUPERMARCHE LYON'
    assert ops['R1'].bank_label == '240101 CB****1234 | SUPERMARCHE  LYON'
    assert ops['R1'].amount == 12.5 and ops['R1'].direction == 'debit'
    assert ops['R2'].direction == 'credit' and ops['R2'].classification is None
    assert conn.status == 'active' and conn.last_sync_at is not None and conn.last_error is None


def test_first_sync_window_90_days_then_overlap_5(setup_a):
    h, conn, acc, cl = setup_a
    today = date(2026, 11, 10)
    bank_sync_service.sync_connection(conn, today=today)
    assert cl.calls[-1][1] == today - timedelta(days=90)
    BankAccountLink.query.one().last_synced_at = datetime(2026, 11, 8, 3, 0)
    bank_sync_service.sync_connection(conn, today=today)
    assert cl.calls[-1][1] == date(2026, 11, 3)


def test_sync_pending_replaced_each_time(setup_a):
    h, conn, acc, cl = setup_a
    cl.txs['uid-1'] = [pdng('5', date.today()), pdng('7', date.today())]
    bank_sync_service.sync_connection(conn)
    assert PendingBankMovement.query.count() == 2
    cl.txs['uid-1'] = [pdng('9', date.today())]
    bank_sync_service.sync_connection(conn)
    rows = PendingBankMovement.query.all()
    assert [r.amount for r in rows] == [9.0]
    cl.txs['uid-1'] = []
    bank_sync_service.sync_connection(conn)
    assert PendingBankMovement.query.count() == 0


def test_sync_triggers_reconciliation(setup_a):
    h, conn, acc, cl = setup_a
    today = date.today()
    from app.models import Category
    cat = Category(pot_id=h.common.id, name='Courses', type='dynamic', percentage=10)
    _db.session.add(cat)
    _db.session.commit()
    manual = Operation(pot_id=h.common.id, user_id=h.user_a.id, category_id=cat.id, amount=12.5,
                       date=today, label='courses', direction='debit', classification='category',
                       entered_amount=12.5)
    _db.session.add(manual)
    _db.session.commit()
    cl.txs['uid-1'] = [book('R1', '12.50', today)]
    res = bank_sync_service.sync_connection(conn)
    assert res.auto_linked == 1 and res.to_classify == 0
    assert Operation.query.count() == 1
    assert Operation.query.one().bank_reference == 'R1'


def test_resync_overlap_no_duplicates(setup_a):
    h, conn, acc, cl = setup_a
    cl.txs['uid-1'] = [book('R1', '3', date.today()), book('R2', '4', date.today())]
    bank_sync_service.sync_connection(conn)
    cl.txs['uid-1'].append(book('R3', '5', date.today()))
    res = bank_sync_service.sync_connection(conn)
    assert res.imported == 1
    assert Operation.query.count() == 3


def test_balances_stored_and_kept_when_absent(setup_a):
    h, conn, acc, cl = setup_a
    cl.bals['uid-1'] = [
        {'balance_type': 'CLBD', 'balance_amount': {'amount': '100.50', 'currency': 'EUR'}},
        {'balance_type': 'OTHR', 'balance_amount': {'amount': '90.00', 'currency': 'EUR'}},
    ]
    bank_sync_service.sync_connection(conn)
    acc = _db.session.get(BankAccount, acc.id)
    assert (acc.balance_booked, acc.balance_value) == (100.5, 90.0)
    first_at = acc.balance_at
    assert first_at is not None
    cl.bals['uid-1'] = [{'balance_type': 'VALU', 'balance_amount': {'amount': '80', 'currency': 'EUR'}}]
    bank_sync_service.sync_connection(conn)
    acc = _db.session.get(BankAccount, acc.id)
    assert (acc.balance_booked, acc.balance_value) == (100.5, 80.0)
    at = acc.balance_at
    cl.bals['uid-1'] = []
    bank_sync_service.sync_connection(conn)
    acc = _db.session.get(BankAccount, acc.id)
    assert (acc.balance_booked, acc.balance_value, acc.balance_at) == (100.5, 80.0, at)


def test_joint_primary_then_fallback(household_ab, fake):
    h = household_ab
    acc = make_account(h.common, 'Joint')
    conn_a = make_conn(h.user_a, 'app-a')
    conn_b = make_conn(h.user_b, 'app-b')
    make_link(conn_a, acc, 'uid-a', primary=True)
    make_link(conn_b, acc, 'uid-b', primary=False)
    tx = [book('R1', '10', date.today())]
    fake['app-a'].txs['uid-a'] = tx
    fake['app-b'].txs['uid-b'] = tx

    bank_sync_service.sync_connection(conn_a)
    res_b = bank_sync_service.sync_connection(conn_b)
    assert fake['app-b'].calls == [] and res_b.imported == 0
    assert Operation.query.count() == 1

    conn_a.status = 'expired'
    _db.session.commit()
    fake['app-b'].txs['uid-b'] = tx + [book('R2', '20', date.today())]
    res_b = bank_sync_service.sync_connection(conn_b)
    assert res_b.imported == 1 and Operation.query.count() == 2


def test_fallback_only_lowest_id_non_primary(household_ab, fake):
    h = household_ab
    acc = make_account(h.common, 'Joint')
    conn_a = make_conn(h.user_a, 'app-a', status='expired')
    conn_b = make_conn(h.user_b, 'app-b')
    third = User(email='c@test.com', display_name='C', household_id=h.user_a.household_id,
                 password_hash='x')
    _db.session.add(third)
    _db.session.commit()
    conn_c = make_conn(third, 'app-c')
    make_link(conn_a, acc, 'uid-a', primary=True)
    make_link(conn_b, acc, 'uid-b', primary=False)
    make_link(conn_c, acc, 'uid-c', primary=False)
    bank_sync_service.sync_connection(conn_b)
    bank_sync_service.sync_connection(conn_c)
    assert len(fake['app-b'].calls) == 1 and fake['app-c'].calls == []


def test_archived_account_not_synced(setup_a):
    h, conn, acc, cl = setup_a
    acc.archived = True
    _db.session.commit()
    bank_sync_service.sync_connection(conn)
    assert cl.calls == []


def test_auth_error_marks_expired(setup_a):
    h, conn, acc, cl = setup_a
    cl.error = EnableBankingAuthError(401, 'EXPIRED_SESSION', 'secret-body')
    res = bank_sync_service.sync_connection(conn)
    assert conn.status == 'expired'
    assert conn.last_error == 'Accès bancaire expiré — reconnectez-vous.'
    assert res.errors == [conn.last_error]


def test_other_error_generic_message(setup_a):
    h, conn, acc, cl = setup_a
    cl.error = EnableBankingError(500, 'BOOM', 'tokentokentoken')
    bank_sync_service.sync_connection(conn)
    assert conn.status == 'error'
    assert conn.last_error == 'Synchro impossible (erreur banque).'
    assert 'BOOM' not in conn.last_error and '500' not in conn.last_error


def test_decrypt_failure_isolated(household_ab, fake, monkeypatch, app):
    h = household_ab
    acc1 = make_account(h.personal_a, 'A')
    acc2 = make_account(h.personal_b, 'B')
    bad = make_conn(h.user_a, 'app-a')
    good = make_conn(h.user_b, 'app-b')
    bad.private_key_enc = b'garbage'
    _db.session.commit()
    make_link(bad, acc1, 'uid-a')
    make_link(good, acc2, 'uid-b')
    fake['app-b'].txs['uid-b'] = [book('R1', '3', date.today())]
    patched = bank_sync_service.get_client
    real = lambda c: secrets_service.decrypt(c.private_key_enc)  # noqa: E731
    monkeypatch.setattr(bank_sync_service, 'get_client',
                        lambda c: real(c) if c.app_id == 'app-a' else patched(c))
    bank_sync_service.sync_all(app)
    _db.session.expire_all()
    assert _db.session.get(BankConnection, bad.id).status == 'error'
    assert _db.session.get(BankConnection, bad.id).last_error == 'Secrets illisibles — redéposez votre clé.'
    assert _db.session.get(BankConnection, good.id).status == 'active'
    assert Operation.query.count() == 1


def test_check_expiries_emails_once(household_ab, monkeypatch, app):
    h = household_ab
    sent = []
    monkeypatch.setattr(email_service, 'send_alert_email', lambda to, s, b: sent.append((to, s, b)))
    soon = make_conn(h.user_a, 'app-a', valid_days=10)
    far = make_conn(h.user_b, 'app-b', valid_days=60)
    assert bank_sync_service.check_expiries(app) == 1
    assert bank_sync_service.check_expiries(app) == 0
    assert len(sent) == 1 and sent[0][0] == ['a@test.com']
    assert soon.valid_until.strftime('%d/%m/%Y') in sent[0][2] and '/bank' in sent[0][2]
    assert _db.session.get(BankConnection, soon.id).expiry_notified_at is not None
    assert _db.session.get(BankConnection, far.id).expiry_notified_at is None


def test_sync_route_flash_and_own_connection_only(household_ab, fake):
    h = household_ab
    acc = make_account(h.common)
    conn_a = make_conn(h.user_a, 'app-a')
    make_link(conn_a, acc, 'uid-1')
    fake['app-a'].txs['uid-1'] = [book('R1', '3', date.today())]
    # B has no connection: nothing happens to A's
    r = h.client_b.post('/bank/sync', follow_redirects=True)
    assert 'Aucune connexion bancaire active' in r.get_data(as_text=True)
    assert fake['app-a'].calls == [] and Operation.query.count() == 0
    r = h.client_a.post('/bank/sync', follow_redirects=True)
    assert '1 importée(s) — 0 rapprochée(s) — 1 à classer' in r.get_data(as_text=True)
    assert Operation.query.count() == 1


def test_sync_button_on_pointage_only_with_active_connection(household_ab, fake):
    h = household_ab
    make_conn(h.user_a, 'app-a')
    assert 'Synchroniser maintenant' in h.client_a.get('/operations/pointage').get_data(as_text=True)
    assert 'Synchroniser maintenant' not in h.client_b.get('/operations/pointage').get_data(as_text=True)


def test_banner_owner_only(household_ab, fake):
    h = household_ab
    make_conn(h.user_a, 'app-a', status='expired')
    page_a = h.client_a.get('/').get_data(as_text=True)
    page_b = h.client_b.get('/').get_data(as_text=True)
    assert 'id="bank-banner"' in page_a and 'id="bank-banner"' not in page_b


def test_banner_expiring_and_healthy(household_ab, fake):
    h = household_ab
    make_conn(h.user_a, 'app-a', valid_days=5)
    make_conn(h.user_b, 'app-b', valid_days=90)
    assert 'expire bientôt' in h.client_a.get('/').get_data(as_text=True)
    assert 'id="bank-banner"' not in h.client_b.get('/').get_data(as_text=True)


def test_pending_section_hidden_for_other_member_personal_accounts(household_ab, fake):
    h = household_ab
    acc_perso = make_account(h.personal_a, 'Perso A')
    acc_common = make_account(h.common, 'Joint')
    _db.session.add_all([
        PendingBankMovement(bank_account_id=acc_perso.id, date=date.today(), amount=11.0,
                            direction='debit', label='SECRET PERSO'),
        PendingBankMovement(bank_account_id=acc_common.id, date=date.today(), amount=22.0,
                            direction='debit', label='VISIBLE COMMUN'),
    ])
    _db.session.commit()
    page_b = h.client_b.get('/operations/pointage').get_data(as_text=True)
    assert 'VISIBLE COMMUN' in page_b and 'En cours à la banque' in page_b
    assert 'SECRET PERSO' not in page_b
    # A in the common pot does not see the personal pending either; in the personal pot, yes
    page_a = h.client_a.get('/operations/pointage').get_data(as_text=True)
    assert 'SECRET PERSO' not in page_a
    h.client_a.post(f'/pots/{h.personal_a.id}/select')
    page_a = h.client_a.get('/operations/pointage').get_data(as_text=True)
    assert 'SECRET PERSO' in page_a and 'VISIBLE COMMUN' not in page_a


def test_error_status_retried_by_daily_job(setup_a, app):
    h, conn, acc, cl = setup_a
    cl.error = EnableBankingError(500, 'BOOM')
    bank_sync_service.sync_connection(conn)
    assert conn.status == 'error'
    cl.error = None
    cl.txs['uid-1'] = [book('R1', '3', date.today())]
    bank_sync_service.sync_all(app)
    _db.session.expire_all()
    conn = _db.session.get(BankConnection, conn.id)
    assert conn.status == 'active' and conn.last_error is None
    assert Operation.query.count() == 1


def test_expired_not_synced_by_daily_job(setup_a, app):
    h, conn, acc, cl = setup_a
    conn.status = 'expired'
    _db.session.commit()
    bank_sync_service.sync_all(app)
    assert cl.calls == []


def test_manual_button_on_error_connection(household_ab, fake):
    h = household_ab
    acc = make_account(h.common)
    conn = make_conn(h.user_a, 'app-a', status='error')
    make_link(conn, acc, 'uid-1')
    assert 'Synchroniser maintenant' in h.client_a.get('/operations/pointage').get_data(as_text=True)
    fake['app-a'].txs['uid-1'] = [book('R1', '3', date.today())]
    h.client_a.post('/bank/sync')
    assert _db.session.get(BankConnection, conn.id).status == 'active'
    assert Operation.query.count() == 1


def test_expired_has_no_button_and_sync_refused(household_ab, fake):
    h = household_ab
    make_conn(h.user_a, 'app-a', status='expired')
    assert 'Synchroniser maintenant' not in h.client_a.get('/operations/pointage').get_data(as_text=True)
    r = h.client_a.post('/bank/sync', follow_redirects=True)
    assert 'Aucune connexion bancaire active' in r.get_data(as_text=True)
    assert fake['app-a'].calls == []


def test_fallback_not_triggered_by_primary_in_error(household_ab, fake):
    h = household_ab
    acc = make_account(h.common, 'Joint')
    conn_a = make_conn(h.user_a, 'app-a', status='error')
    conn_b = make_conn(h.user_b, 'app-b')
    make_link(conn_a, acc, 'uid-a', primary=True)
    make_link(conn_b, acc, 'uid-b', primary=False)
    bank_sync_service.sync_connection(conn_b)
    assert fake['app-b'].calls == []


def test_sync_next_redirects(household_ab, fake):
    h = household_ab
    make_conn(h.user_a, 'app-a')
    r = h.client_a.post('/bank/sync', data={'next': 'pointage'})
    assert r.headers['Location'].endswith('/operations/pointage')
    r = h.client_a.post('/bank/sync', data={'next': 'https://evil.example'})
    assert r.headers['Location'].endswith('/bank')


def test_check_expiries_skips_expired(household_ab, monkeypatch, app):
    sent = []
    monkeypatch.setattr(email_service, 'send_alert_email', lambda *a: sent.append(a))
    make_conn(household_ab.user_a, 'app-a', status='expired', valid_days=-1)
    assert bank_sync_service.check_expiries(app) == 0 and sent == []


def test_sync_all_survives_generic_exception(household_ab, fake, monkeypatch, app):
    h = household_ab
    acc1 = make_account(h.personal_a, 'A')
    acc2 = make_account(h.personal_b, 'B')
    c1 = make_conn(h.user_a, 'app-a')
    c2 = make_conn(h.user_b, 'app-b')
    make_link(c1, acc1, 'uid-a')
    make_link(c2, acc2, 'uid-b')
    fake['app-b'].txs['uid-b'] = [book('R1', '3', date.today())]
    real = bank_sync_service.sync_connection

    def flaky(conn, today=None):
        if conn.app_id == 'app-a':
            raise RuntimeError('boom')
        return real(conn, today)

    monkeypatch.setattr(bank_sync_service, 'sync_connection', flaky)
    bank_sync_service.sync_all(app)
    assert Operation.query.count() == 1


# --- final review fixes ------------------------------------------------------

def test_new_link_after_first_sync_gets_full_window(household_ab, fake):
    h = household_ab
    conn = make_conn(h.user_a, 'app-a', last_sync=datetime(2026, 11, 8, 3, 0))
    acc = make_account(h.common)
    make_link(conn, acc, 'uid-1')
    today = date(2026, 11, 10)
    bank_sync_service.sync_connection(conn, today=today)
    assert fake['app-a'].calls[-1][1] == today - timedelta(days=90)
    link = BankAccountLink.query.one()
    assert link.last_synced_at is not None
    bank_sync_service.sync_connection(conn, today=today)
    assert fake['app-a'].calls[-1][1] == link.last_synced_at.date() - timedelta(days=5)


def test_first_sync_starts_after_latest_existing_bank_date(household_ab, fake):
    h = household_ab
    acc = make_account(h.common)
    _db.session.add(Operation(pot_id=h.common.id, user_id=h.user_a.id, bank_account_id=acc.id, bank_reference='CSV1',
                              bank_date=date(2026, 11, 1), date=date(2026, 11, 1), amount=5,
                              direction='debit', label='x', bank_label='x'))
    _db.session.commit()
    conn = make_conn(h.user_a, 'app-a')
    make_link(conn, acc, 'uid-1')
    bank_sync_service.sync_connection(conn, today=date(2026, 11, 10))
    assert fake['app-a'].calls[-1][1] == date(2026, 11, 2)


def test_fallback_link_takes_over_from_account_latest_bank_date(household_ab, fake):
    h = household_ab
    acc = make_account(h.common, 'Joint')
    conn_a = make_conn(h.user_a, 'app-a', status='expired')
    conn_b = make_conn(h.user_b, 'app-b', last_sync=datetime(2026, 11, 9, 3, 0))
    make_link(conn_a, acc, 'uid-a', primary=True)
    make_link(conn_b, acc, 'uid-b', primary=False)
    _db.session.add(Operation(pot_id=h.common.id, user_id=h.user_a.id, bank_account_id=acc.id, bank_reference='A1',
                              bank_date=date(2026, 11, 6), date=date(2026, 11, 6), amount=5,
                              direction='debit', label='x', bank_label='x'))
    _db.session.commit()
    bank_sync_service.sync_connection(conn_b, today=date(2026, 11, 10))
    assert fake['app-b'].calls[-1][1] == date(2026, 11, 7)


def test_orphaned_link_not_synced(setup_a):
    h, conn, acc, cl = setup_a
    BankAccountLink.query.one().orphaned = True
    _db.session.commit()
    bank_sync_service.sync_connection(conn)
    assert cl.calls == []


def test_orphaned_primary_does_not_block_fallback(household_ab, fake):
    h = household_ab
    acc = make_account(h.common, 'Joint')
    conn_a = make_conn(h.user_a, 'app-a')
    conn_b = make_conn(h.user_b, 'app-b')
    la = make_link(conn_a, acc, 'uid-a', primary=True)
    make_link(conn_b, acc, 'uid-b', primary=False)
    la.orphaned = True
    _db.session.commit()
    bank_sync_service.sync_connection(conn_b)
    assert len(fake['app-b'].calls) == 1
