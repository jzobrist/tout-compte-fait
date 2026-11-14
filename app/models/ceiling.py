from app.extensions import db


class EnvelopePlan(db.Model):
    """Frozen spending ceilings of a pot for one budget month (the « plan du mois »)."""
    __tablename__ = 'envelope_plan'
    __table_args__ = (
        db.UniqueConstraint('pot_id', 'year', 'month', name='uq_envelope_plan_pot_year_month'),
    )

    id = db.Column(db.Integer, primary_key=True)
    pot_id = db.Column(db.Integer, db.ForeignKey('pot.id'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    validated_at = db.Column(db.DateTime, nullable=True)

    pot = db.relationship('Pot')
    ceilings = db.relationship('EnvelopeCeiling', back_populates='plan', cascade='all, delete-orphan')


class EnvelopeCeiling(db.Model):
    """Ceiling of one envelope within an EnvelopePlan."""
    __tablename__ = 'envelope_ceiling'
    __table_args__ = (
        db.UniqueConstraint('plan_id', 'category_id', name='uq_envelope_ceiling_plan_category'),
    )

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('envelope_plan.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id', ondelete='CASCADE'), nullable=False)
    amount = db.Column(db.Double, nullable=False)

    plan = db.relationship('EnvelopePlan', back_populates='ceilings')
