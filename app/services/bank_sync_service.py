"""Enable Banking connection: setup, consent flow, account mapping (sync lives further down)."""
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from flask import abort, has_app_context

from app.extensions import db
from app.models import BankAccount, Operation, BankAccountLink, BankConnection, PendingBankMovement, Pot
from app.services import bank_labels, secrets_service
from app.services.access_service import accessible_pots, can_access_pot, pot_from_form_or_404
from app.services.enablebanking_client import (
    EnableBankingAuthError, EnableBankingClient, validate_private_key)
from app.services.import_service import MovementIn, ingest_movements
from app.services.secrets_service import SecretsError

CONSENT_DAYS = 180
SYNCABLE = ('active', 'error')  # 'error' is retried; 'expired'/'setup' need a (re)connection


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_client(connection) -> EnableBankingClient:
    """The only place a client is built (tests monkeypatch it)."""
    pem = secrets_service.decrypt(connection.private_key_enc)
    return EnableBankingClient(connection.app_id, pem)


def get_connection(user):
    return BankConnection.query.filter_by(user_id=user.id).first()


def save_setup(user, app_id: str, pem: str, aspsp_name: str) -> BankConnection:
    """Validate and store the user's credentials (raises ValueError on an invalid key).
    Replacing credentials drops any previous session."""
    validate_private_key(pem)
    encrypted = secrets_service.encrypt(pem)
    conn = get_connection(user)
    if conn is None:
        conn = BankConnection(user_id=user.id)
        db.session.add(conn)
    conn.app_id = app_id.strip()
    conn.private_key_enc = encrypted
    conn.aspsp_name = aspsp_name.strip()
    conn.status = 'setup'
    conn.session_id_enc = None
    conn.valid_until = None
    conn.auth_state = None
    conn.last_error = None
    conn.expiry_notified_at = None
    db.session.commit()
    return conn


def start_connection(connection, redirect_url: str) -> str:
    state = str(uuid.uuid4())
    valid_until = datetime.now(timezone.utc) + timedelta(days=CONSENT_DAYS)
    url = get_client(connection).start_auth(connection.aspsp_name, redirect_url, state, valid_until)
    connection.auth_state = state
    db.session.commit()
    return url


def _parse_dt(raw):
    try:
        dt = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _find_account_by_fingerprint(user, fingerprint):
    pot_ids = [p.id for p in accessible_pots(user)]
    return (BankAccount.query
            .filter(BankAccount.pot_id.in_(pot_ids), BankAccount.iban_fingerprint == fingerprint,
                    BankAccount.archived.is_(False))
            .order_by(BankAccount.id).first())


def _has_primary(account_id):
    return (BankAccountLink.query
            .filter_by(bank_account_id=account_id, is_primary=True, orphaned=False).first()
            is not None)


def _purge_pending_if_unsourced(account_id):
    """Drop the pending movements of an account no live link feeds any more."""
    live = BankAccountLink.query.filter_by(bank_account_id=account_id, orphaned=False,
                                           ignored=False).first()
    if live is None:
        PendingBankMovement.query.filter_by(bank_account_id=account_id).delete()


def _orphan(link):
    """The bank no longer offers this account: stop syncing it, hand over the primary role."""
    link.orphaned = True
    account_id = link.bank_account_id
    if account_id is None:
        link.is_primary = False
        return
    if link.is_primary:
        link.is_primary = False
        db.session.flush()
        heir = (BankAccountLink.query
                .filter(BankAccountLink.bank_account_id == account_id,
                        BankAccountLink.id != link.id,
                        BankAccountLink.orphaned.is_(False), BankAccountLink.ignored.is_(False))
                .order_by(BankAccountLink.id).first())
        if heir is not None:
            heir.is_primary = True
    db.session.flush()
    _purge_pending_if_unsourced(account_id)


def complete_connection(user, state: str, code: str) -> BankConnection:
    conn = get_connection(user)
    if conn is None or not state or not conn.auth_state or conn.auth_state != state:
        raise PermissionError('Unknown or foreign connection state')
    session = get_client(conn).create_session(code)
    conn.session_id_enc = secrets_service.encrypt(session['session_id'])
    valid_until = _parse_dt((session.get('access') or {}).get('valid_until'))
    conn.valid_until = valid_until or _utcnow() + timedelta(days=CONSENT_DAYS)
    conn.status = 'active'
    conn.auth_state = None
    conn.last_error = None
    conn.expiry_notified_at = None

    accounts = [a for a in session.get('accounts', []) if a.get('uid')]
    session_uids = {a['uid'] for a in accounts}
    seen_ids = set()
    for acc in accounts:
        uid = acc['uid']
        iban = ''.join(((acc.get('account_id') or {}).get('iban') or '').split())
        fingerprint = secrets_service.iban_fingerprint(iban) if iban else None
        link = BankAccountLink.query.filter_by(connection_id=conn.id, provider_uid=uid).first()
        if link is None and fingerprint:
            # Same account under a new provider uid (reconnection): keep the existing link
            link = (BankAccountLink.query
                    .filter(BankAccountLink.connection_id == conn.id,
                            BankAccountLink.iban_fingerprint == fingerprint,
                            BankAccountLink.provider_uid.notin_(session_uids),
                            BankAccountLink.id.notin_(seen_ids or {0}))
                    .order_by(BankAccountLink.id).first())
            if link is not None:
                link.provider_uid = uid
        if link is None:
            link = BankAccountLink(connection_id=conn.id, provider_uid=uid)
            db.session.add(link)
        if link.orphaned:
            link.orphaned = False
            if link.bank_account_id is not None and not _has_primary(link.bank_account_id):
                link.is_primary = True
        link.iban_fingerprint = fingerprint or link.iban_fingerprint
        link.iban_last4 = iban[-4:] if iban else link.iban_last4
        link.label = (acc.get('name') or acc.get('product') or 'Compte')[:120]
        if link.bank_account_id is None and not link.ignored and link.iban_fingerprint:
            match = _find_account_by_fingerprint(user, link.iban_fingerprint)
            if match is not None:
                link.bank_account_id = match.id
                link.is_primary = not _has_primary(match.id)
        db.session.flush()
        seen_ids.add(link.id)
    for link in list(conn.links):
        if link.id not in seen_ids and not link.orphaned:
            _orphan(link)
    db.session.commit()
    return conn


def attachable_accounts(user):
    """Existing accounts a session account may be attached to: accessible pots, not archived,
    never fed by a bank yet (no fingerprint) and without a live primary link."""
    pot_ids = [p.id for p in accessible_pots(user)]
    accounts = (BankAccount.query
                .filter(BankAccount.pot_id.in_(pot_ids), BankAccount.archived.is_(False),
                        BankAccount.iban_fingerprint.is_(None))
                .order_by(BankAccount.pot_id, BankAccount.id).all())
    return [a for a in accounts if not _has_primary(a.id)]


def map_account(user, link, pot_id, name, ignore: bool, account_id=None):
    """Map a session account. Returns the already-known account when a fingerprint match
    overrode the choice, else None."""
    if (link.connection.user_id != user.id or link.bank_account_id is not None or link.ignored
            or link.orphaned):
        abort(404)
    if ignore:
        link.ignored = True
        link.bank_account_id = None
        link.is_primary = False
        db.session.commit()
        return None
    existing = _find_account_by_fingerprint(user, link.iban_fingerprint) if link.iban_fingerprint else None
    if existing is not None:
        # Another member already mapped this (joint) account: attach, never duplicate.
        link.bank_account_id = existing.id
        link.is_primary = not _has_primary(existing.id)
        db.session.commit()
        return existing
    if account_id:
        account = db.session.get(BankAccount, int(account_id))
        if (account is None or account.archived or not can_access_pot(user, account.pot)
                or account.iban_fingerprint is not None or _has_primary(account.id)):
            abort(404)
        account.iban_fingerprint = link.iban_fingerprint
        link.bank_account_id = account.id
        link.is_primary = True
        db.session.commit()
        return None
    pot = pot_from_form_or_404(pot_id)
    account = BankAccount(
        pot_id=pot.id,
        name=(name or '').strip() or link.label or 'Compte',
        bank_name=link.connection.aspsp_name,
        account_number_hint=link.iban_last4,
        iban_fingerprint=link.iban_fingerprint,
    )
    db.session.add(account)
    db.session.flush()
    link.bank_account_id = account.id
    link.is_primary = True
    link.ignored = False
    db.session.commit()
    return None


def delete_connection(user) -> None:
    conn = get_connection(user)
    if conn is not None:
        account_ids = {l.bank_account_id for l in conn.links if l.bank_account_id is not None}
        db.session.delete(conn)  # links cascade; BankAccounts and operations stay
        db.session.flush()
        for account_id in account_ids:
            _purge_pending_if_unsourced(account_id)
        db.session.commit()


# ---------------------------------------------------------------- sync

FIRST_SYNC_DAYS = 90
OVERLAP_DAYS = 5
EXPIRY_NOTICE_DAYS = 14

MSG_EXPIRED = 'Accès bancaire expiré — reconnectez-vous.'
MSG_BANK_ERROR = 'Synchro impossible (erreur banque).'
MSG_SECRETS = 'Secrets illisibles — redéposez votre clé.'


@dataclass
class SyncResult:
    imported: int = 0
    auto_linked: int = 0
    to_classify: int = 0
    pending: int = 0
    errors: list = field(default_factory=list)


def _links_to_sync(connection):
    """Links this connection must import: primary links, or (primary's connection not active)
    the lowest-id non-primary link with an active connection."""
    selected = []
    for link in sorted(connection.links, key=lambda l: l.id):
        account = link.bank_account
        if link.ignored or link.orphaned or account is None or account.archived:
            continue
        if link.is_primary:
            selected.append(link)
            continue
        primary = BankAccountLink.query.filter_by(bank_account_id=account.id, is_primary=True,
                                                  orphaned=False).first()
        if primary is not None and primary.connection.status not in ('expired', 'setup'):
            continue
        candidates = (BankAccountLink.query.join(BankConnection)
                      .filter(BankAccountLink.bank_account_id == account.id,
                              BankAccountLink.is_primary.is_(False),
                              BankAccountLink.ignored.is_(False),
                              BankAccountLink.orphaned.is_(False),
                              BankConnection.status.in_(SYNCABLE))
                      .order_by(BankAccountLink.id).first())
        if candidates is not None and candidates.id == link.id:
            selected.append(link)
    return selected


def _amount(raw_amount):
    return float((raw_amount or {}).get('amount'))


def _movement(tx):
    lines = tx.get('remittance_information') or []
    return MovementIn(
        reference=bank_labels.reference_for(tx),
        booked_on=date.fromisoformat(tx['booking_date']),
        amount=abs(_amount(tx.get('transaction_amount'))),
        direction='credit' if tx.get('credit_debit_indicator') == 'CRDT' else 'debit',
        label=bank_labels.reconstruct(lines),
        bank_label=bank_labels.raw(lines),
    )


def _pending(tx, account, today):
    lines = tx.get('remittance_information') or []
    raw_date = tx.get('booking_date') or tx.get('value_date') or tx.get('transaction_date')
    return PendingBankMovement(
        bank_account_id=account.id,
        date=date.fromisoformat(raw_date) if raw_date else today,
        amount=abs(_amount(tx.get('transaction_amount'))),
        direction='credit' if tx.get('credit_debit_indicator') == 'CRDT' else 'debit',
        label=(bank_labels.reconstruct(lines) or bank_labels.raw(lines))[:200],
    )


def _store_balances(account, balances, now):
    by_type = {}
    for b in balances or []:
        try:
            by_type.setdefault(b.get('balance_type'), _amount(b.get('balance_amount')))
        except (TypeError, ValueError):
            continue
    changed = False
    if 'CLBD' in by_type:
        account.balance_booked = by_type['CLBD']
        changed = True
    value = by_type.get('VALU', by_type.get('OTHR'))
    if value is not None:
        account.balance_value = value
        changed = True
    if changed:
        account.balance_at = now


def _window_start(link, today):
    """Per link: last sync - 5 days; first sync: the day after the account's latest bank movement
    (history already there, e.g. from a CSV), else 90 days back."""
    if link.last_synced_at is not None:
        return link.last_synced_at.date() - timedelta(days=OVERLAP_DAYS)
    latest = (db.session.query(db.func.max(Operation.bank_date))
              .filter(Operation.bank_account_id == link.bank_account_id,
                      Operation.bank_reference.isnot(None)).scalar())
    if latest is not None:
        return latest + timedelta(days=1)
    return today - timedelta(days=FIRST_SYNC_DAYS)


def _sync_link(client, link, user, today, now, result):
    account = link.bank_account
    date_from = _window_start(link, today)
    txs = client.transactions(link.provider_uid, date_from)
    movements, pending = [], []
    for tx in txs:
        try:
            if tx.get('status') == 'PDNG':
                pending.append(_pending(tx, account, today))
            elif tx.get('status') == 'BOOK':
                movements.append(_movement(tx))
        except (TypeError, ValueError, KeyError):
            continue  # malformed transaction: skip it, never abort the account
    res = ingest_movements(account, movements, user)
    result.imported += res.imported
    result.auto_linked += res.auto_linked
    result.to_classify += res.to_classify
    PendingBankMovement.query.filter_by(bank_account_id=account.id).delete()
    db.session.add_all(pending)
    result.pending += len(pending)
    _store_balances(account, client.balances(link.provider_uid), now)
    link.last_synced_at = now
    db.session.commit()


def sync_connection(connection, today=None) -> SyncResult:
    today = today or date.today()
    now = _utcnow()
    result = SyncResult()
    try:
        client = get_client(connection)
        for link in _links_to_sync(connection):
            _sync_link(client, link, connection.user, today, now, result)
    except EnableBankingAuthError:
        status, message = 'expired', MSG_EXPIRED
    except SecretsError:
        status, message = 'error', MSG_SECRETS
    except Exception:  # EnableBankingError and anything unexpected: never leak the text
        status, message = 'error', MSG_BANK_ERROR
    else:
        connection.last_sync_at = now
        connection.status = 'active'
        connection.last_error = None
        db.session.commit()
        _check_free_money(connection, today)
        return result
    db.session.rollback()
    connection.status = status
    connection.last_error = message
    db.session.commit()
    result.errors.append(message)
    return result


def _check_free_money(connection, today):
    """Free-money alerts for the pots whose accounts this connection just synced (never fatal)."""
    from app.services import alert_service
    pot_ids = sorted({link.bank_account.pot_id for link in _links_to_sync(connection)
                      if link.bank_account is not None})
    for pot_id in pot_ids:
        try:
            alert_service.check_free_money_alerts(db.session.get(Pot, pot_id), today)
        except Exception:
            db.session.rollback()


@contextmanager
def _app_ctx(app):
    if has_app_context():
        yield
    else:
        with app.app_context():
            yield


def sync_all(app) -> None:
    with _app_ctx(app):
        ids = [c.id for c in BankConnection.query.filter(BankConnection.status.in_(SYNCABLE))]
        for conn_id in ids:
            try:
                conn = db.session.get(BankConnection, conn_id)
                if conn is not None and conn.status in SYNCABLE:
                    sync_connection(conn)
            except Exception:
                db.session.rollback()
                app.logger.error('Bank sync failed for connection %s', conn_id)


def check_expiries(app, now=None) -> int:
    from app.services.email_service import send_alert_email
    now = now or _utcnow()
    sent = 0
    with _app_ctx(app):
        base = (app.config.get('APP_BASE_URL')
                or (app.config.get('BANK_REDIRECT_URL') or '').replace('/bank/callback', '')
                or '').rstrip('/')
        threshold = now + timedelta(days=EXPIRY_NOTICE_DAYS)
        due = BankConnection.query.filter(BankConnection.valid_until.isnot(None),
                                          BankConnection.valid_until <= threshold,
                                          BankConnection.expiry_notified_at.is_(None),
                                          BankConnection.status != 'expired').all()
        for conn in due:
            body = (f"Bonjour,\n\nL'accès à votre banque ({conn.aspsp_name}) expire le "
                    f"{conn.valid_until.strftime('%d/%m/%Y')}. Reconnectez-vous avant cette date "
                    f"pour que la synchronisation continue :\n{base}/bank\n")
            try:
                send_alert_email([conn.user.email], 'Tout Compte Fait : accès bancaire bientôt expiré', body)
            except Exception:
                app.logger.error('Expiry e-mail failed for connection %s', conn.id)
                continue
            conn.expiry_notified_at = now
            db.session.commit()
            sent += 1
    return sent


def banner_state(user, now=None, conn=None):
    """'expired' | 'error' | 'expiring' | None for the user's own connection."""
    conn = conn or get_connection(user)
    if conn is None:
        return None
    if conn.status in ('expired', 'error'):
        return conn.status
    now = now or _utcnow()
    if conn.valid_until is not None and conn.valid_until <= now + timedelta(days=EXPIRY_NOTICE_DAYS):
        return 'expiring'
    return None
