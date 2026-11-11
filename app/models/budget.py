from app.extensions import db

class BudgetMonth(db.Model):
    __tablename__ = 'budget_month'

    id = db.Column(db.Integer, primary_key=True)
    pot_id = db.Column(db.Integer, db.ForeignKey('pot.id'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    total_income = db.Column(db.Float, default=0.0, nullable=False)

    pot = db.relationship('Pot')
    category_budgets = db.relationship('BudgetMonthCategory', back_populates='budget_month',
                                        lazy='dynamic', cascade='all, delete-orphan')

    __table_args__ = (db.UniqueConstraint('pot_id', 'year', 'month', name='uq_budget_month_pot_year_month'),)


class BudgetMonthCategory(db.Model):
    __tablename__ = 'budget_month_category'

    id = db.Column(db.Integer, primary_key=True)
    budget_month_id = db.Column(db.Integer, db.ForeignKey('budget_month.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    # base_allocated = budget_brut + carry_in, before manual adjustments
    base_allocated = db.Column(db.Float, default=0.0, nullable=False)
    carry_in = db.Column(db.Float, default=0.0, nullable=False)
    # Closed for the month: unspent remainder is free now and is not carried over
    closed_at = db.Column(db.DateTime, nullable=True)

    budget_month = db.relationship('BudgetMonth', back_populates='category_budgets')
    category = db.relationship('Category', back_populates='budget_month_categories')
