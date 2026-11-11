from datetime import datetime, timezone

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ContributionPlan(db.Model):
    """Contribution au commun of one common-pot budget month (J6)."""
    __tablename__ = 'contribution_plan'
    __table_args__ = (
        db.UniqueConstraint('pot_id', 'year', 'month', name='uq_contribution_plan_pot_year_month'),
    )

    id = db.Column(db.Integer, primary_key=True)
    pot_id = db.Column(db.Integer, db.ForeignKey('pot.id'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    position = db.Column(db.Double, nullable=False, default=0.0)
    forced = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(10), nullable=False, default='draft')  # 'draft' | 'validated'
    validated_negative = db.Column(db.Boolean, nullable=False, default=False)
    solidarity_adjusted = db.Column(db.Boolean, nullable=False, default=False, server_default='0')
    validated_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    pot = db.relationship('Pot')
    validations = db.relationship('ContributionValidation', back_populates='plan', cascade='all, delete-orphan')
    shares = db.relationship('ContributionShare', back_populates='plan', cascade='all, delete-orphan')
    allocations = db.relationship('ProvisionAllocation', back_populates='plan', cascade='all, delete-orphan')
    caps = db.relationship('ContributionCap', back_populates='plan', cascade='all, delete-orphan')


class ContributionValidation(db.Model):
    __tablename__ = 'contribution_validation'
    __table_args__ = (db.UniqueConstraint('plan_id', 'user_id', name='uq_contribution_validation'),)

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('contribution_plan.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    snapshot = db.Column(db.String(500), nullable=False, default='')

    plan = db.relationship('ContributionPlan', back_populates='validations')


class ContributionShare(db.Model):
    __tablename__ = 'contribution_share'
    __table_args__ = (db.UniqueConstraint('plan_id', 'user_id', name='uq_contribution_share'),)

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('contribution_plan.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Double, nullable=False, default=0.0)

    plan = db.relationship('ContributionPlan', back_populates='shares')


class ProvisionAllocation(db.Model):
    __tablename__ = 'provision_allocation'
    __table_args__ = (db.UniqueConstraint('plan_id', 'category_id', name='uq_provision_allocation'),)

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('contribution_plan.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id', ondelete='CASCADE'), nullable=False)
    amount = db.Column(db.Double, nullable=False, default=0.0)

    plan = db.relationship('ContributionPlan', back_populates='allocations')


class ContributionCap(db.Model):
    """Transfer cap measured for a member when the plan was prepared (None = unknown balance)."""
    __tablename__ = 'contribution_cap'
    __table_args__ = (db.UniqueConstraint('plan_id', 'user_id', name='uq_contribution_cap'),)

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('contribution_plan.id', ondelete='CASCADE'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    cap = db.Column(db.Double, nullable=True)

    plan = db.relationship('ContributionPlan', back_populates='caps')
