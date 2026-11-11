from datetime import datetime, timezone
from app.extensions import db

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class AlertLog(db.Model):
    __tablename__ = 'alert_log'

    id = db.Column(db.Integer, primary_key=True)
    pot_id = db.Column(db.Integer, db.ForeignKey('pot.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    # 'overspending' | 'ahead'
    alert_type = db.Column(db.String(20), nullable=False)
    sent_at = db.Column(db.DateTime, default=_utcnow)

    pot = db.relationship('Pot')
