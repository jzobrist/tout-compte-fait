from datetime import datetime, timezone
from app.extensions import db

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class BudgetAdjustment(db.Model):
    __tablename__ = 'budget_adjustment'

    id = db.Column(db.Integer, primary_key=True)
    budget_month_id = db.Column(db.Integer, db.ForeignKey('budget_month.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    from_category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    to_category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    amount = db.Column(db.Double, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    from_category = db.relationship('Category', foreign_keys=[from_category_id])
    to_category = db.relationship('Category', foreign_keys=[to_category_id])
