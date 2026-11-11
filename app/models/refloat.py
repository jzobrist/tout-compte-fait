from datetime import datetime, timezone

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RefloatPlan(db.Model):
    """Remise à flot of a pot for one budget month: deficit frozen at measurement time."""
    __tablename__ = 'refloat_plan'
    __table_args__ = (
        db.UniqueConstraint('pot_id', 'year', 'month', name='uq_refloat_plan_pot_year_month'),
    )

    id = db.Column(db.Integer, primary_key=True)
    pot_id = db.Column(db.Integer, db.ForeignKey('pot.id'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    deficit = db.Column(db.Float, nullable=False, default=0.0)
    months = db.Column(db.Integer, nullable=False, default=1)
    split = db.Column(db.Float, nullable=False, default=0.5)  # common pot: share paid by transfers
    created_at = db.Column(db.DateTime, default=_utcnow)

    pot = db.relationship('Pot')
    cuts = db.relationship('RefloatCut', back_populates='plan', cascade='all, delete-orphan')


class RefloatCut(db.Model):
    """Manual reduction of one dynamic envelope, overriding the automatic cut."""
    __tablename__ = 'refloat_cut'
    __table_args__ = (
        db.UniqueConstraint('plan_id', 'category_id', name='uq_refloat_cut_plan_category'),
    )

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('refloat_plan.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id', ondelete='CASCADE'), nullable=False)
    amount = db.Column(db.Float, nullable=False, default=0.0)

    plan = db.relationship('RefloatPlan', back_populates='cuts')


class PotAlertState(db.Model):
    """Whether a threshold alert ('free_negative' | 'free_overdraft') is currently active for a pot."""
    __tablename__ = 'pot_alert_state'
    __table_args__ = (db.UniqueConstraint('pot_id', 'alert_type', name='uq_pot_alert_state'),)

    id = db.Column(db.Integer, primary_key=True)
    pot_id = db.Column(db.Integer, db.ForeignKey('pot.id'), nullable=False)
    alert_type = db.Column(db.String(20), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=False)
