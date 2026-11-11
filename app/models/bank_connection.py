from datetime import datetime, timezone
from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BankConnection(db.Model):
    __tablename__ = 'bank_connection'
    __table_args__ = (db.UniqueConstraint('user_id', name='uq_bank_connection_user_id'),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    app_id = db.Column(db.String(64), nullable=False)
    private_key_enc = db.Column(db.LargeBinary, nullable=False)
    aspsp_name = db.Column(db.String(120), nullable=False)
    session_id_enc = db.Column(db.LargeBinary, nullable=True)
    valid_until = db.Column(db.DateTime, nullable=True)
    # setup | active | expired | error
    status = db.Column(db.String(20), nullable=False, default='setup', server_default='setup')
    auth_state = db.Column(db.String(64), nullable=True)
    last_sync_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.String(255), nullable=True)
    expiry_notified_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User')
    links = db.relationship('BankAccountLink', back_populates='connection',
                            cascade='all, delete-orphan')


class BankAccountLink(db.Model):
    __tablename__ = 'bank_account_link'
    __table_args__ = (
        db.UniqueConstraint('connection_id', 'provider_uid', name='uq_bank_link_connection_uid'),
    )

    id = db.Column(db.Integer, primary_key=True)
    connection_id = db.Column(db.Integer, db.ForeignKey('bank_connection.id'), nullable=False)
    bank_account_id = db.Column(db.Integer, db.ForeignKey('bank_account.id'), nullable=True)
    provider_uid = db.Column(db.String(100), nullable=False)
    is_primary = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    ignored = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    # Kept so the account can be mapped after the callback (the full IBAN is never stored)
    iban_fingerprint = db.Column(db.String(64), nullable=True)
    iban_last4 = db.Column(db.String(4), nullable=True)
    label = db.Column(db.String(120), nullable=True)
    # Absent from the latest session (account no longer offered by the bank): never synced
    orphaned = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    last_synced_at = db.Column(db.DateTime, nullable=True)

    connection = db.relationship('BankConnection', back_populates='links')
    bank_account = db.relationship('BankAccount')


class PendingBankMovement(db.Model):
    __tablename__ = 'pending_bank_movement'

    id = db.Column(db.Integer, primary_key=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey('bank_account.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    direction = db.Column(db.String(10), nullable=False)
    label = db.Column(db.String(200), nullable=True)

    bank_account = db.relationship('BankAccount')
