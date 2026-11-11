import secrets
from datetime import datetime, timedelta, timezone
from app.extensions import db

def _utcnow():
    """Return current UTC time as naive datetime (SQLite compatibility)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

class Household(db.Model):
    __tablename__ = 'household'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    invite_code = db.Column(db.String(64), unique=True, nullable=True)
    invite_expires_at = db.Column(db.DateTime, nullable=True)
    solidarity_margin = db.Column(db.Float, nullable=False, default=0.0, server_default='0')

    users = db.relationship('User', back_populates='household', lazy='dynamic')

    pots = db.relationship('Pot', back_populates='household', lazy='select')

    def generate_invite(self, expires_hours=48):
        self.invite_code = secrets.token_urlsafe(32)
        self.invite_expires_at = _utcnow() + timedelta(hours=expires_hours)

    def is_invite_valid(self):
        if not self.invite_code or not self.invite_expires_at:
            return False
        return _utcnow() < self.invite_expires_at

    def invalidate_invite(self):
        self.invite_code = None
        self.invite_expires_at = None
