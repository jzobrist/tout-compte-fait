from sqlalchemy import event, update
from sqlalchemy.orm.attributes import set_committed_value
from app.extensions import db

class Category(db.Model):
    __tablename__ = 'category'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    pot_id = db.Column(db.Integer, db.ForeignKey('pot.id'), nullable=False)
    # 'fixed_auto' | 'fixed_amount' | 'dynamic'
    type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=True)          # fixed_auto, fixed_amount
    percentage = db.Column(db.Float, nullable=True)      # dynamic (0.0–1.0)
    min_amount = db.Column(db.Float, nullable=True)      # dynamic floor
    max_amount = db.Column(db.Float, nullable=True)      # dynamic ceiling
    carry_over = db.Column(db.Boolean, default=True, nullable=False)
    auto_day = db.Column(db.Integer, nullable=True)      # fixed_auto: day of month
    # personal pot, fixed types: counts against the member's reste à vivre (J6)
    constrained = db.Column(db.Boolean, default=False, nullable=False)
    # common pot: may receive the common surplus (J6)
    provision = db.Column(db.Boolean, default=False, nullable=False)
    # 'common_transfer' on the personal « Virement vers le commun » category
    role = db.Column(db.String(20), nullable=True)
    # Family of categories across pots; set by the after_insert hook (an INSERT cannot know its own id)
    family_id = db.Column(db.Integer, nullable=True, index=True)
    # 'shared' | 'own' | None
    scope = db.Column(db.String(10), nullable=True)

    pot = db.relationship('Pot', back_populates='categories')
    operations = db.relationship('Operation', back_populates='category', lazy='dynamic', cascade='save-update, merge')
    refloat_cuts = db.relationship('RefloatCut', cascade='all, delete-orphan')
    allocations = db.relationship('ProvisionAllocation', cascade='all, delete-orphan')
    budget_month_categories = db.relationship('BudgetMonthCategory', back_populates='category', lazy='dynamic', cascade='all, delete-orphan')


@event.listens_for(Category, 'after_insert')
def _default_family(mapper, connection, target):
    if target.family_id is None:
        connection.execute(
            update(Category.__table__).where(Category.__table__.c.id == target.id).values(family_id=target.id))
        set_committed_value(target, 'family_id', target.id)
