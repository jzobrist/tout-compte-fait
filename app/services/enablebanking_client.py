"""Thin client for the Enable Banking API (JWT RS256 auth)."""
import json
import re
import time
from datetime import date, datetime, timezone

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

BASE_URL = 'https://api.enablebanking.com'
TIMEOUT = 30
# 4xx error codes (besides 401/403) meaning the session/consent is expired, closed or invalid.
# Deliberately explicit: e.g. a 400 about valid_until / maximum consent validity is NOT in it.
AUTH_ERROR_CODES = frozenset({
    'EXPIRED_SESSION', 'SESSION_EXPIRED', 'INVALID_SESSION', 'SESSION_CLOSED',
    'SESSION_NOT_FOUND', 'CONSENT_EXPIRED', 'CONSENT_REVOKED', 'CONSENT_INVALID',
    'EXPIRED_CONSENT', 'REVOKED_CONSENT', 'INVALID_CONSENT',
})
_CODE_RE = re.compile(r'[^A-Z0-9_]')


def _error_code(body_text):
    """Short sanitized error code from a JSON error body ('' if none); never free text."""
    try:
        data = json.loads(body_text)
    except (ValueError, TypeError):
        return ''
    if not isinstance(data, dict):
        return ''
    raw = data.get('error') or data.get('code')
    if not isinstance(raw, str):
        return ''
    return _CODE_RE.sub('', raw.upper())[:40]


class EnableBankingError(Exception):
    """`message` is built from status and a sanitized error code only; the raw provider
    body lives on `.body` and must never be logged or rendered."""

    def __init__(self, status, message='', body=''):
        super().__init__(f'Enable Banking error {status}' + (f': {message}' if message else ''))
        self.status = status
        self.message = message
        self.body = body

    def __repr__(self):
        return f'{type(self).__name__}({self.status!r}, {self.message!r})'


class EnableBankingAuthError(EnableBankingError):
    pass


def validate_private_key(pem: str) -> None:
    """Raise ValueError unless pem is an unencrypted RSA private key."""
    try:
        key = load_pem_private_key(pem.encode('utf-8'), password=None)
    except Exception as exc:  # public key, garbage, encrypted PEM...
        raise ValueError('Not a valid unencrypted private key') from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError('Private key must be RSA')


class EnableBankingClient:
    def __init__(self, app_id: str, private_key_pem: str, base_url: str = BASE_URL):
        self.app_id = app_id
        self._pem = private_key_pem
        self.base_url = base_url.rstrip('/')

    def _headers(self):
        now = int(time.time())
        token = jwt.encode(
            {'iss': 'enablebanking.com', 'aud': 'api.enablebanking.com',
             'iat': now, 'exp': now + 3600},
            self._pem, algorithm='RS256', headers={'kid': self.app_id})
        return {'Authorization': f'Bearer {token}'}

    def _call(self, method, path, **kwargs):
        try:
            resp = requests.request(method, self.base_url + path, headers=self._headers(),
                                    timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise EnableBankingError(0, 'NETWORK_ERROR') from exc
        if resp.status_code >= 400:
            body = (resp.text or '')[:300]
            code = _error_code(resp.text or '')
            if resp.status_code in (401, 403) or (
                    400 <= resp.status_code < 500 and code in AUTH_ERROR_CODES):
                raise EnableBankingAuthError(resp.status_code, code, body)
            raise EnableBankingError(resp.status_code, code, body)
        try:
            return resp.json()
        except ValueError as exc:
            raise EnableBankingError(resp.status_code, 'INVALID_RESPONSE') from exc

    def aspsps(self, country='FR'):
        return self._call('GET', '/aspsps', params={'country': country}).get('aspsps', [])

    def start_auth(self, aspsp_name, redirect_url, state, valid_until: datetime) -> str:
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        valid_until = valid_until.astimezone(timezone.utc)
        body = {
            'access': {'valid_until': valid_until.isoformat()},
            'aspsp': {'name': aspsp_name, 'country': 'FR'},
            'state': state,
            'redirect_url': redirect_url,
            'psu_type': 'personal',
        }
        return self._call('POST', '/auth', json=body)['url']

    def create_session(self, code) -> dict:
        return self._call('POST', '/sessions', json={'code': code})

    def balances(self, account_uid) -> list:
        return self._call('GET', f'/accounts/{account_uid}/balances').get('balances', [])

    def transactions(self, account_uid, date_from: date) -> list:
        txs, key = [], None
        while True:
            params = {'date_from': date_from.isoformat()}
            if key:
                params['continuation_key'] = key
            page = self._call('GET', f'/accounts/{account_uid}/transactions', params=params)
            txs.extend(page.get('transactions', []))
            key = page.get('continuation_key')
            if not key:
                return txs
