from datetime import datetime, timezone
from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BankAccount(db.Model):
    __tablename__ = 'bank_account'

    id = db.Column(db.Integer, primary_key=True)
    pot_id = db.Column(db.Integer, db.ForeignKey('pot.id'), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    bank_name = db.Column(db.String(120), nullable=False)
    account_number_hint = db.Column(db.String(50), nullable=True)
    overdraft_limit = db.Column(db.Float, nullable=False, default=0.0)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    iban_fingerprint = db.Column(db.String(64), nullable=True, index=True)
    balance_booked = db.Column(db.Float, nullable=True)
    balance_value = db.Column(db.Float, nullable=True)
    balance_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    pot = db.relationship('Pot', back_populates='bank_accounts')
