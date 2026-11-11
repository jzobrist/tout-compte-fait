import io
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.extensions import db as _db
from app.models import BankAccount, BankConnection, BankAccountLink
from app.services import bank_sync_service, secrets_service
from app.services.enablebanking_client import EnableBankingError

IBAN_PERSO = 'FR7612345678901234567894321'
IBAN_JOINT = 'FR7612345678901234567898765'
IBAN_PERSO_B = 'FR7698765432109876543212222'


def _pem(public=False, password=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if public:
        return key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    enc = (serialization.BestAvailableEncryption(password) if password
           else serialization.NoEncryption())
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8, enc)


class FakeClient:
    sessions = {}  # app_id -> accounts list

    def __init__(self, app_id):
        self.app_id = app_id
        self.started = []

    def aspsps(self, country='FR'):
        return [{'name': 'Boursorama'}, {'name': 'Autre'}]

    def start_auth(self, aspsp_name, redirect_url, state, valid_until):
        self.started.append((aspsp_name, redirect_url, state, valid_until))
        return f'https://bank.example/auth?state={state}'

    def create_session(self, code):
        return {'session_id': f'sess-{code}', 'accounts': self.sessions[self.app_id]}


@pytest.fixture
def fake(monkeypatch):
    clients = {}

    def get_client(connection):
        return clients.setdefault(connection.app_id, FakeClient(connection.app_id))

    monkeypatch.setattr(bank_sync_service, 'get_client', get_client)
    FakeClient.sessions = {
        'app-a': [
            {'uid': 'uid-perso-a', 'account_id': {'iban': IBAN_PERSO}, 'name': 'Compte courant'},
            {'uid': 'uid-joint-a', 'account_id': {'iban': IBAN_JOINT}, 'name': 'Compte joint'},
        ],
        'app-b': [
            {'uid': 'uid-joint-b', 'account_id': {'iban': IBAN_JOINT}, 'name': 'Compte joint'},
            {'uid': 'uid-perso-b', 'account_id': {'iban': IBAN_PERSO_B}, 'name': 'Livret'},
        ],
    }
    return clients


def _setup(client, app_id='app-a', aspsp='Boursorama', pem=None):
    return client.post('/bank/setup', data={
        'app_id': app_id, 'aspsp_name': aspsp,
        'pem': (io.BytesIO(pem or _pem()), 'key.pem'),
    }, content_type='multipart/form-data')


def _connect(client, user_email, code='c1'):
    client.post('/bank/connect')
    conn = BankConnection.query.join(BankConnection.user).filter_by(email=user_email).one()
    state = conn.auth_state
    r = client.get(f'/bank/callback?state={state}&code={code}')
    return r, state


def test_setup_stores_encrypted_key(household_ab):
    pem = _pem()
    r = _setup(household_ab.client_a, pem=pem)
    assert r.status_code == 302
    conn = BankConnection.query.one()
    assert conn.user_id == household_ab.user_a.id
    assert conn.status == 'setup'
    assert b'BEGIN' not in conn.private_key_enc
    assert secrets_service.decrypt(conn.private_key_enc) == pem.decode()


@pytest.mark.parametrize('kind', ['public', 'garbage', 'encrypted'])
def test_invalid_pem_rejected(household_ab, kind):
    pem = {'public': _pem(public=True), 'garbage': b'hello world',
           'encrypted': _pem(password=b'pw')}[kind]
    r = _setup(household_ab.client_a, pem=pem)
    assert r.status_code == 302
    assert BankConnection.query.count() == 0
    page = household_ab.client_a.get('/bank').get_data(as_text=True)
    assert 'clé privée' in page


def test_connect_redirects_to_bank_url(household_ab, fake):
    _setup(household_ab.client_a)
    r = household_ab.client_a.post('/bank/connect')
    assert r.status_code == 302
    conn = BankConnection.query.one()
    assert conn.auth_state
    assert r.headers['Location'] == f'https://bank.example/auth?state={conn.auth_state}'
    aspsp, redirect_url, state, valid_until = fake['app-a'].started[0]
    assert aspsp == 'Boursorama' and state == conn.auth_state
    assert redirect_url.endswith('/bank/callback')
    delta = valid_until - datetime.now(timezone.utc)
    assert timedelta(days=179) < delta <= timedelta(days=180)


def test_callback_state_of_other_user_refused(household_ab, fake):
    _setup(household_ab.client_a)
    household_ab.client_a.post('/bank/connect')
    state = BankConnection.query.one().auth_state
    r = household_ab.client_b.get(f'/bank/callback?state={state}&code=x')
    assert r.status_code == 302 and r.headers['Location'].endswith('/bank')
    page = household_ab.client_b.get('/bank').get_data(as_text=True)
    assert 'Demande de connexion inconnue ou expirée.' in page
    conn = BankConnection.query.one()
    assert conn.auth_state == state and conn.status == 'setup' and conn.session_id_enc is None
    assert BankAccountLink.query.count() == 0


def test_callback_unknown_state_refused(household_ab, fake):
    r = household_ab.client_a.get('/bank/callback?state=nope&code=x')
    assert r.status_code == 302
    assert 'Demande de connexion inconnue' in household_ab.client_a.get('/bank').get_data(as_text=True)


def test_callback_success_activates_and_lists_accounts(household_ab, fake):
    _setup(household_ab.client_a)
    r, _ = _connect(household_ab.client_a, 'a@test.com')
    assert r.status_code == 302
    conn = BankConnection.query.one()
    assert conn.status == 'active' and conn.auth_state is None
    assert b'sess-' not in conn.session_id_enc
    assert secrets_service.decrypt(conn.session_id_enc) == 'sess-c1'
    assert conn.valid_until is not None
    links = BankAccountLink.query.order_by(BankAccountLink.provider_uid).all()
    assert [l.provider_uid for l in links] == ['uid-joint-a', 'uid-perso-a']
    assert all(l.bank_account_id is None for l in links)
    page = household_ab.client_a.get('/bank').get_data(as_text=True)
    assert '4321' in page and '8765' in page
    assert IBAN_PERSO not in page and 'app-a' in page


def test_full_iban_never_stored(household_ab, fake):
    _setup(household_ab.client_a)
    _connect(household_ab.client_a, 'a@test.com')
    for l in BankAccountLink.query.all():
        for v in (l.iban_fingerprint, l.iban_last4, l.label, l.provider_uid):
            assert IBAN_PERSO not in (v or '') and IBAN_JOINT not in (v or '')
        assert len(l.iban_last4) == 4


def test_callback_paste_parses_return_url(household_ab, fake):
    _setup(household_ab.client_a)
    household_ab.client_a.post('/bank/connect')
    state = BankConnection.query.one().auth_state
    url = 'https://tcf.example/bank/callback?' + urlencode({'state': state, 'code': 'zz'})
    r = household_ab.client_a.post('/bank/callback-paste', data={'return_url': url})
    assert r.status_code == 302
    conn = BankConnection.query.one()
    assert conn.status == 'active'
    assert secrets_service.decrypt(conn.session_id_enc) == 'sess-zz'


def test_callback_paste_garbage_refused(household_ab, fake):
    _setup(household_ab.client_a)
    household_ab.client_a.post('/bank/connect')
    r = household_ab.client_a.post('/bank/callback-paste', data={'return_url': 'pas une url'})
    assert r.status_code == 302
    assert BankConnection.query.one().status == 'setup'


def _map(client, link, pot, name=None, ignore=False):
    data = {'pot_id': pot.id if pot else '', 'name': name or ''}
    if ignore:
        data['ignore'] = '1'
    return client.post(f'/bank/links/{link.id}/map', data=data)


def _link(uid):
    return BankAccountLink.query.filter_by(provider_uid=uid).one()


def test_map_personal_and_joint(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    assert _map(h.client_a, _link('uid-perso-a'), h.personal_a, 'Courant').status_code == 302
    assert _map(h.client_a, _link('uid-joint-a'), h.common, 'Joint').status_code == 302
    perso = BankAccount.query.filter_by(pot_id=h.personal_a.id).one()
    joint = BankAccount.query.filter_by(pot_id=h.common.id).one()
    assert perso.name == 'Courant' and perso.bank_name == 'Boursorama'
    assert perso.account_number_hint == '4321' and joint.account_number_hint == '8765'
    assert perso.iban_fingerprint == secrets_service.iban_fingerprint(IBAN_PERSO)
    assert _link('uid-perso-a').bank_account_id == perso.id and _link('uid-perso-a').is_primary
    assert _link('uid-joint-a').is_primary


def test_map_ignore(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    _map(h.client_a, _link('uid-perso-a'), None, ignore=True)
    assert _link('uid-perso-a').ignored and _link('uid-perso-a').bank_account_id is None
    assert BankAccount.query.count() == 0


def test_b_joint_auto_attached_by_fingerprint(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    _map(h.client_a, _link('uid-joint-a'), h.common, 'Joint')
    _setup(h.client_b, app_id='app-b')
    _connect(h.client_b, 'b@test.com')
    joint = BankAccount.query.filter_by(pot_id=h.common.id).one()
    lb = _link('uid-joint-b')
    assert lb.bank_account_id == joint.id and lb.is_primary is False
    assert _link('uid-joint-a').is_primary is True
    lp = _link('uid-perso-b')
    assert lp.bank_account_id is None and not lp.ignored


def test_fingerprint_in_other_members_personal_pot_ignored(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    _map(h.client_a, _link('uid-perso-a'), h.personal_a, 'Courant')
    # B's session happens to expose A's personal IBAN
    FakeClient.sessions['app-b'] = [
        {'uid': 'uid-x', 'account_id': {'iban': IBAN_PERSO}, 'name': 'X'}]
    _setup(h.client_b, app_id='app-b')
    _connect(h.client_b, 'b@test.com')
    lx = _link('uid-x')
    assert lx.bank_account_id is None
    page = h.client_b.get('/bank').get_data(as_text=True)
    assert 'Courant' not in page


def test_map_into_inaccessible_pot_404(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    r = _map(h.client_a, _link('uid-perso-a'), h.personal_b, 'Piège')
    assert r.status_code == 404
    assert BankAccount.query.count() == 0


def test_map_foreign_link_404(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    r = _map(h.client_b, _link('uid-perso-a'), h.personal_b, 'Vol')
    assert r.status_code == 404
    assert BankAccount.query.count() == 0


def test_b_sees_nothing_of_a_connection(household_ab, fake):
    h = household_ab
    _setup(h.client_a, app_id='app-a', aspsp='BanqueSecrete')
    _connect(h.client_a, 'a@test.com')
    _map(h.client_a, _link('uid-perso-a'), h.personal_a, 'Perso secret A')
    page = h.client_b.get('/bank').get_data(as_text=True)
    for leaked in ('app-a', 'BanqueSecrete', '4321', '8765', 'Perso secret A', 'Compte joint'):
        assert leaked not in page
    assert 'Connecter ma banque' not in page  # no connection yet: setup form only


def test_delete_connection_keeps_accounts(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    _map(h.client_a, _link('uid-joint-a'), h.common, 'Joint')
    r = h.client_a.post('/bank/delete')
    assert r.status_code == 302
    assert BankConnection.query.count() == 0 and BankAccountLink.query.count() == 0
    assert BankAccount.query.filter_by(pot_id=h.common.id).count() == 1


def test_delete_does_not_touch_other_connection(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _setup(h.client_b, app_id='app-b')
    h.client_b.post('/bank/delete')
    assert BankConnection.query.one().user_id == h.user_a.id


def test_bank_requires_login(client):
    assert client.get('/bank').status_code == 302
    assert client.get('/bank/callback?state=a&code=b').status_code == 302


def test_connect_error_is_generic(household_ab, fake, monkeypatch):
    _setup(household_ab.client_a)

    def boom(self, *a, **k):
        raise EnableBankingError(400, 'BAD', body='SECRET-BODY')
    monkeypatch.setattr(FakeClient, 'start_auth', boom)
    r = household_ab.client_a.post('/bank/connect')
    assert r.status_code == 302 and r.headers['Location'].endswith('/bank')
    assert 'SECRET-BODY' not in household_ab.client_a.get('/bank').get_data(as_text=True)


def test_reconnect_reuses_links_by_uid(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    _map(h.client_a, _link('uid-joint-a'), h.common, 'Joint')
    n = BankAccountLink.query.count()
    _connect(h.client_a, 'a@test.com', code='c2')
    assert BankAccountLink.query.count() == n
    assert _link('uid-joint-a').bank_account_id is not None
    assert secrets_service.decrypt(BankConnection.query.one().session_id_enc) == 'sess-c2'


# --- fix round 1 -------------------------------------------------------------

from html.parser import HTMLParser


class _FormParser(HTMLParser):
    """Collects the fields (first option selected by default) of the form posting to `action`."""

    def __init__(self, action):
        super().__init__()
        self.action, self.inside, self.fields, self._select = action, False, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self.inside = a.get('action') == self.action
        elif self.inside and tag == 'input' and a.get('name'):
            if a.get('type') in ('checkbox', 'radio') and 'checked' not in a:
                return
            self.fields.append((a['name'], a.get('value', '')))
        elif self.inside and tag == 'select':
            self._select = a['name']
            self.fields.append((self._select, None))
        elif self.inside and tag == 'option' and self._select:
            if self.fields[-1][1] is None or 'selected' in a:
                self.fields[-1] = (self._select, a.get('value', ''))

    def handle_endtag(self, tag):
        if tag == 'form':
            self.inside = False
        elif tag == 'select':
            self._select = None


def test_rendered_map_form_posts_successfully(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    link = _link('uid-perso-a')
    parser = _FormParser(f'/bank/links/{link.id}/map')
    parser.feed(h.client_a.get('/bank').get_data(as_text=True))
    from werkzeug.datastructures import MultiDict
    # placeholder preselected: the user must choose a pot, like a browser would
    assert [v for k, v in parser.fields if k == 'pot_id'] == ['']
    data = MultiDict([(k, v) for k, v in parser.fields if k != 'pot_id'])  # keeps duplicates
    data.add('pot_id', str(h.personal_a.id))
    r = h.client_a.post(f'/bank/links/{link.id}/map', data=data)
    assert r.status_code == 302
    assert BankAccount.query.count() == 1
    assert _link('uid-perso-a').bank_account_id is not None


def test_map_twice_creates_one_account(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    link = _link('uid-joint-a')
    assert _map(h.client_a, link, h.common, 'Joint').status_code == 302
    assert _map(h.client_a, link, h.common, 'Joint').status_code == 404
    assert BankAccount.query.count() == 1


def test_map_ignored_link_404(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    link = _link('uid-joint-a')
    _map(h.client_a, link, None, ignore=True)
    assert _map(h.client_a, link, h.common, 'Joint').status_code == 404
    assert BankAccount.query.count() == 0


def test_both_map_joint_one_account_one_primary(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    _setup(h.client_b, app_id='app-b')
    _connect(h.client_b, 'b@test.com')
    assert _map(h.client_a, _link('uid-joint-a'), h.common, 'Joint').status_code == 302
    assert _map(h.client_b, _link('uid-joint-b'), h.common, 'Joint').status_code == 302
    assert BankAccount.query.filter_by(pot_id=h.common.id).count() == 1
    links = [_link('uid-joint-a'), _link('uid-joint-b')]
    assert links[0].bank_account_id == links[1].bank_account_id
    assert sum(l.is_primary for l in links) == 1


def test_aspsp_failure_falls_back_to_text_input(household_ab, fake, monkeypatch):
    _setup(household_ab.client_a)

    def boom(self, country='FR'):
        raise RuntimeError('SECRET')
    monkeypatch.setattr(FakeClient, 'aspsps', boom)
    r = household_ab.client_a.get('/bank')
    assert r.status_code == 200 and 'datalist' not in r.get_data(as_text=True)


@pytest.fixture
def csrf_app_client():
    from app import create_app
    app = create_app('testing')
    app.config['WTF_CSRF_ENABLED'] = True
    with app.app_context():
        _db.create_all()
        yield app.test_client()
        _db.session.remove()
        _db.drop_all()


@pytest.mark.parametrize('path', ['/bank/setup', '/bank/connect', '/bank/callback-paste',
                                  '/bank/links/1/map', '/bank/delete'])
def test_bank_posts_require_csrf(csrf_app_client, path):
    from app.models import User
    from app.services.household_service import create_household
    u = User(email='c@test.com', display_name='C')
    u.set_password('Password1!')
    create_household('F', u)
    _db.session.commit()
    with csrf_app_client.session_transaction() as sess:
        sess['_user_id'] = str(u.id)
        sess['_fresh'] = True
    assert csrf_app_client.post(path, data={}).status_code == 400


# --- final review fixes ------------------------------------------------------

def _reconnect_with(sessions, h, code='c2'):
    FakeClient.sessions['app-a'] = sessions
    _connect(h.client_a, 'a@test.com', code=code)


def test_reconnect_with_changed_uid_updates_same_link(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    _map(h.client_a, _link('uid-joint-a'), h.common, 'Joint')
    old = _link('uid-joint-a')
    old_id, acc_id = old.id, old.bank_account_id
    _reconnect_with([
        {'uid': 'uid-joint-NEW', 'account_id': {'iban': IBAN_JOINT}, 'name': 'Compte joint'},
        {'uid': 'uid-perso-a', 'account_id': {'iban': IBAN_PERSO}, 'name': 'Compte courant'},
    ], h)
    link = _link('uid-joint-NEW')
    assert link.id == old_id and link.bank_account_id == acc_id and link.is_primary
    assert not link.orphaned
    assert BankAccountLink.query.count() == 2
    assert BankAccountLink.query.filter_by(provider_uid='uid-joint-a').first() is None


def test_reconnect_missing_account_orphans_link_and_promotes_other(household_ab, fake):
    from app.models import PendingBankMovement
    from datetime import date
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    _setup(h.client_b, app_id='app-b')
    _connect(h.client_b, 'b@test.com')
    _map(h.client_a, _link('uid-joint-a'), h.common, 'Joint')
    _map(h.client_b, _link('uid-joint-b'), h.common, 'Joint')
    a_link = _link('uid-joint-a')
    assert a_link.is_primary
    _db.session.add(PendingBankMovement(bank_account_id=a_link.bank_account_id, date=date.today(),
                                        amount=1, direction='debit', label='x'))
    _db.session.commit()
    _reconnect_with([{'uid': 'uid-perso-a', 'account_id': {'iban': IBAN_PERSO}, 'name': 'C'}], h)
    a_link = _link('uid-joint-a')
    assert a_link.orphaned and not a_link.is_primary
    assert _link('uid-joint-b').is_primary
    assert PendingBankMovement.query.count() == 1  # b still feeds the account


def test_orphaned_only_source_purges_pending(household_ab, fake):
    from app.models import PendingBankMovement
    from datetime import date
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    _map(h.client_a, _link('uid-joint-a'), h.common, 'Joint')
    acc_id = _link('uid-joint-a').bank_account_id
    _db.session.add(PendingBankMovement(bank_account_id=acc_id, date=date.today(),
                                        amount=1, direction='debit', label='x'))
    _db.session.commit()
    _reconnect_with([{'uid': 'uid-perso-a', 'account_id': {'iban': IBAN_PERSO}, 'name': 'C'}], h)
    assert PendingBankMovement.query.count() == 0


def test_delete_connection_purges_pending(household_ab, fake):
    from app.models import PendingBankMovement
    from datetime import date
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    _map(h.client_a, _link('uid-joint-a'), h.common, 'Joint')
    acc_id = _link('uid-joint-a').bank_account_id
    _db.session.add(PendingBankMovement(bank_account_id=acc_id, date=date.today(),
                                        amount=1, direction='debit', label='x'))
    _db.session.commit()
    h.client_a.post('/bank/delete')
    assert PendingBankMovement.query.count() == 0


def _attach(client, link, account):
    return client.post(f'/bank/links/{link.id}/map', data={'pot_id': account.pot_id,
                                                           'account_id': account.id})


def test_attach_to_existing_account_sets_fingerprint(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    acc = BankAccount(pot_id=h.common.id, name='Manuel', bank_name='B')
    _db.session.add(acc)
    _db.session.commit()
    page = h.client_a.get('/bank').get_data(as_text=True)
    assert 'Manuel' in page and 'Rattacher à un compte existant' in page
    assert _attach(h.client_a, _link('uid-joint-a'), acc).status_code == 302
    link = _link('uid-joint-a')
    assert link.bank_account_id == acc.id and link.is_primary
    assert acc.iban_fingerprint == link.iban_fingerprint
    assert BankAccount.query.count() == 1


def test_attach_refused_for_archived_inaccessible_or_taken(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    archived = BankAccount(pot_id=h.common.id, name='Vieux', bank_name='B', archived=True)
    foreign = BankAccount(pot_id=h.personal_b.id, name='Autre', bank_name='B')
    _db.session.add_all([archived, foreign])
    _db.session.commit()
    assert _attach(h.client_a, _link('uid-joint-a'), archived).status_code == 404
    assert _attach(h.client_a, _link('uid-joint-a'), foreign).status_code == 404
    assert _link('uid-joint-a').bank_account_id is None


def test_fingerprint_match_flashes_known_account(household_ab, fake):
    h = household_ab
    _setup(h.client_a)
    _connect(h.client_a, 'a@test.com')
    _setup(h.client_b, app_id='app-b')
    _connect(h.client_b, 'b@test.com')
    _map(h.client_a, _link('uid-joint-a'), h.common, 'Joint')
    r = h.client_b.post(f'/bank/links/{_link("uid-joint-b").id}/map',
                        data={'pot_id': h.personal_b.id, 'name': 'x'}, follow_redirects=True)
    assert 'Compte déjà connu : rattaché à Joint' in r.get_data(as_text=True)
    assert BankAccount.query.count() == 1


def test_aspsp_listed_only_when_editing(household_ab, fake, monkeypatch):
    _setup(household_ab.client_a)
    _connect(household_ab.client_a, 'a@test.com')
    calls = []
    monkeypatch.setattr(FakeClient, 'aspsps', lambda self, country='FR': calls.append(1) or [])
    household_ab.client_a.get('/bank')
    assert calls == []
    household_ab.client_a.get('/bank?edit=1')
    assert calls == [1]
