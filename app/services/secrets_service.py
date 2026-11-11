"""Encryption of bank secrets and keyed IBAN fingerprints (master key: BANK_SECRETS_KEY)."""
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


class SecretsError(Exception):
    pass


def _key() -> bytes:
    key = current_app.config.get('BANK_SECRETS_KEY')
    if not key:
        raise SecretsError('BANK_SECRETS_KEY is not configured')
    return key.encode() if isinstance(key, str) else key


def _fernet() -> Fernet:
    try:
        return Fernet(_key())
    except (ValueError, TypeError) as exc:
        raise SecretsError('BANK_SECRETS_KEY is not a valid Fernet key') from exc


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode('utf-8'))


def decrypt(token: bytes) -> str:
    try:
        return _fernet().decrypt(token).decode('utf-8')
    except (InvalidToken, TypeError, ValueError) as exc:
        raise SecretsError('Unable to decrypt secret') from exc


def iban_fingerprint(iban: str) -> str:
    normalized = ''.join(iban.split()).upper()
    return hmac.new(_key(), normalized.encode('utf-8'), hashlib.sha256).hexdigest()
