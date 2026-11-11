from datetime import date, datetime, timezone

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import create_app
from app.services import secrets_service
from app.services import enablebanking_client as eb


def _rsa_pem(public=False, password=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if public:
        return key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    enc = (serialization.BestAvailableEncryption(password) if password
           else serialization.NoEncryption())
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc).decode()


def test_encrypt_roundtrip_and_ciphertext_differs(app):
    token = secrets_service.encrypt('secret-value')
    assert isinstance(token, bytes)
    assert b'secret-value' not in token
    assert secrets_service.decrypt(token) == 'secret-value'


def test_decrypt_with_other_key_raises(app, monkeypatch):
    token = secrets_service.encrypt('x')
    monkeypatch.setitem(app.config, 'BANK_SECRETS_KEY', Fernet.generate_key().decode())
    with pytest.raises(secrets_service.SecretsError):
        secrets_service.decrypt(token)


def test_missing_key_raises_secrets_error(app, monkeypatch):
    monkeypatch.setitem(app.config, 'BANK_SECRETS_KEY', None)
    with pytest.raises(secrets_service.SecretsError):
        secrets_service.encrypt('x')
    with pytest.raises(secrets_service.SecretsError):
        secrets_service.decrypt(b'x')


def test_iban_fingerprint_normalizes_and_is_keyed(app, monkeypatch):
    a = secrets_service.iban_fingerprint('fr76 1234')
    assert a == secrets_service.iban_fingerprint('FR761234')
    assert len(a) == 64
    monkeypatch.setitem(app.config, 'BANK_SECRETS_KEY', Fernet.generate_key().decode())
    assert secrets_service.iban_fingerprint('FR761234') != a


def test_production_requires_bank_secrets_key(monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'prod-secret')
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///:memory:')
    monkeypatch.delenv('BANK_SECRETS_KEY', raising=False)
    import config
    monkeypatch.setattr(config.ProductionConfig, 'SECRET_KEY', 'prod-secret')
    monkeypatch.setattr(config.ProductionConfig, 'SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
    monkeypatch.setattr(config.ProductionConfig, 'BANK_SECRETS_KEY', None, raising=False)
    with pytest.raises(RuntimeError, match='BANK_SECRETS_KEY'):
        create_app('production')


class FakeResp:
    def __init__(self, status=200, payload=None, text=''):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or str(self._payload)

    def json(self):
        return self._payload


def test_client_jwt_claims(monkeypatch):
    pem = _rsa_pem()
    seen = {}

    def fake_request(method, url, **kw):
        seen['headers'] = kw['headers']
        seen['timeout'] = kw.get('timeout')
        return FakeResp(payload={'aspsps': [{'name': 'Bank'}]})

    monkeypatch.setattr(eb.requests, 'request', fake_request)
    client = eb.EnableBankingClient('app-123', pem)
    assert client.aspsps() == [{'name': 'Bank'}]
    token = seen['headers']['Authorization'].removeprefix('Bearer ')
    assert jwt.get_unverified_header(token)['kid'] == 'app-123'
    assert seen['timeout'] == 30
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    pub = load_pem_private_key(pem.encode(), None).public_key()
    claims = jwt.decode(token, pub, algorithms=['RS256'], audience='api.enablebanking.com')
    assert claims['iss'] == 'enablebanking.com'
    assert claims['exp'] - claims['iat'] <= 3600


def test_client_start_auth_and_session(monkeypatch):
    calls = []

    def fake_request(method, url, **kw):
        calls.append((method, url, kw.get('json')))
        if url.endswith('/auth'):
            return FakeResp(payload={'url': 'https://bank/auth'})
        return FakeResp(payload={'session_id': 's1', 'accounts': [], 'access': {'valid_until': 'x'}})

    monkeypatch.setattr(eb.requests, 'request', fake_request)
    client = eb.EnableBankingClient('a', _rsa_pem())
    until = datetime(2027, 5, 1, tzinfo=timezone.utc)
    assert client.start_auth('Bank', 'https://r', 'st', until) == 'https://bank/auth'
    body = calls[0][2]
    assert body['aspsp'] == {'name': 'Bank', 'country': 'FR'}
    assert body['psu_type'] == 'personal' and body['state'] == 'st'
    assert client.create_session('code')['session_id'] == 's1'
    assert calls[1][2] == {'code': 'code'}


def test_client_transactions_follows_continuation(monkeypatch):
    params_seen = []

    def fake_request(method, url, **kw):
        params_seen.append(kw.get('params'))
        if len(params_seen) == 1:
            return FakeResp(payload={'transactions': [{'n': 1}], 'continuation_key': 'k2'})
        return FakeResp(payload={'transactions': [{'n': 2}], 'continuation_key': None})

    monkeypatch.setattr(eb.requests, 'request', fake_request)
    client = eb.EnableBankingClient('a', _rsa_pem())
    txs = client.transactions('uid', date(2026, 1, 1))
    assert txs == [{'n': 1}, {'n': 2}]
    assert params_seen[0] == {'date_from': '2026-01-01'}
    assert params_seen[1] == {'date_from': '2026-01-01', 'continuation_key': 'k2'}


def test_error_message_never_contains_provider_body(monkeypatch):
    body = '{"error":"WRONG_REQUEST","message":"session sess-SECRET123 iban FR7612345678901234567890123"}'
    monkeypatch.setattr(eb.requests, 'request', lambda *a, **k: FakeResp(400, text=body))
    client = eb.EnableBankingClient('a', _rsa_pem())
    with pytest.raises(eb.EnableBankingError) as e:
        client.balances('uid')
    for text in (str(e.value), repr(e.value)):
        assert 'SECRET123' not in text and 'FR76' not in text
    assert 'WRONG_REQUEST' in str(e.value)
    assert 'SECRET123' in e.value.body


def test_error_code_is_sanitized(monkeypatch):
    body = '{"error":"bad thing <script> with a very long text that goes on and on and on"}'
    monkeypatch.setattr(eb.requests, 'request', lambda *a, **k: FakeResp(400, text=body))
    with pytest.raises(eb.EnableBankingError) as e:
        eb.EnableBankingClient('a', _rsa_pem()).balances('uid')
    assert len(e.value.message) <= 40
    assert all(c.isupper() or c.isdigit() or c == '_' for c in e.value.message)


def test_valid_until_400_is_not_auth_error(monkeypatch):
    body = '{"error":"WRONG_REQUEST_PARAMETERS","message":"valid_until exceeds maximum consent validity"}'
    monkeypatch.setattr(eb.requests, 'request', lambda *a, **k: FakeResp(400, text=body))
    with pytest.raises(eb.EnableBankingError) as e:
        eb.EnableBankingClient('a', _rsa_pem()).balances('uid')
    assert not isinstance(e.value, eb.EnableBankingAuthError)


def test_non_json_success_raises_client_error(monkeypatch):
    class Bad(FakeResp):
        def json(self):
            raise ValueError('no json')
    monkeypatch.setattr(eb.requests, 'request', lambda *a, **k: Bad(200, text='<html>'))
    with pytest.raises(eb.EnableBankingError):
        eb.EnableBankingClient('a', _rsa_pem()).balances('uid')


def test_start_auth_valid_until_is_utc_aware(monkeypatch):
    seen = []
    monkeypatch.setattr(eb.requests, 'request',
                        lambda m, u, **k: seen.append(k['json']) or FakeResp(payload={'url': 'u'}))
    client = eb.EnableBankingClient('a', _rsa_pem())
    client.start_auth('B', 'r', 's', datetime(2027, 5, 1, 12, 0))
    assert seen[0]['access']['valid_until'].endswith('+00:00')


def test_production_rejects_malformed_bank_secrets_key(monkeypatch):
    import config
    monkeypatch.setattr(config.ProductionConfig, 'SECRET_KEY', 'prod-secret')
    monkeypatch.setattr(config.ProductionConfig, 'SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
    monkeypatch.setattr(config.ProductionConfig, 'BANK_SECRETS_KEY', 'not-a-fernet-key', raising=False)
    with pytest.raises(RuntimeError, match='BANK_SECRETS_KEY'):
        create_app('production')


@pytest.mark.parametrize('status', [401, 403])
def test_client_maps_401_to_auth_error(monkeypatch, status):
    monkeypatch.setattr(eb.requests, 'request', lambda *a, **k: FakeResp(status, text='nope'))
    client = eb.EnableBankingClient('a', _rsa_pem())
    with pytest.raises(eb.EnableBankingAuthError) as e:
        client.balances('uid')
    assert e.value.status == status


def test_client_consent_error_and_other_error(monkeypatch):
    client = eb.EnableBankingClient('a', _rsa_pem())
    monkeypatch.setattr(eb.requests, 'request',
                        lambda *a, **k: FakeResp(422, text='{"error":"SESSION_EXPIRED"}'))
    with pytest.raises(eb.EnableBankingAuthError):
        client.balances('uid')
    monkeypatch.setattr(eb.requests, 'request', lambda *a, **k: FakeResp(500, text='boom'))
    with pytest.raises(eb.EnableBankingError) as e:
        client.balances('uid')
    assert not isinstance(e.value, eb.EnableBankingAuthError)


def test_validate_private_key_rejects_public_and_garbage():
    eb.validate_private_key(_rsa_pem())
    with pytest.raises(ValueError):
        eb.validate_private_key(_rsa_pem(public=True))
    with pytest.raises(ValueError):
        eb.validate_private_key('not a pem')
    with pytest.raises(ValueError):
        eb.validate_private_key(_rsa_pem(password=b'pw'))


def test_bank_models_and_constraints(app, db, household_ab):
    from sqlalchemy.exc import IntegrityError
    from app.models import BankConnection, BankAccountLink, PendingBankMovement, BankAccount
    conn = BankConnection(user_id=household_ab.user_a.id, app_id='a', private_key_enc=b'x',
                          aspsp_name='B')
    db.session.add(conn)
    db.session.commit()
    assert conn.status == 'setup'
    acc = BankAccount(pot_id=household_ab.common.id, name='n', bank_name='b',
                      iban_fingerprint='f', balance_booked=1.0)
    db.session.add(acc)
    db.session.commit()
    db.session.add(BankAccountLink(connection_id=conn.id, bank_account_id=acc.id, provider_uid='u'))
    db.session.add(PendingBankMovement(bank_account_id=acc.id, date=date(2026, 1, 1),
                                       amount=1.0, direction='debit', label='l'))
    db.session.commit()
    assert conn.links[0].is_primary is False
    db.session.add(BankAccountLink(connection_id=conn.id, provider_uid='u'))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_reference_longer_than_50_is_hashed():
    from app.services import bank_labels
    long_ref = 'X' * 80
    ref = bank_labels.reference_for({'entry_reference': long_ref})
    assert ref.startswith('h:') and len(ref) <= 50
    assert ref == bank_labels.reference_for({'entry_reference': long_ref})
    assert bank_labels.reference_for({'entry_reference': 'short'}) == 'short'
