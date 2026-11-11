from app.extensions import db


class Pot(db.Model):
    __tablename__ = 'pot'

    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False)
    # 'common' | 'personal'
    kind = db.Column(db.String(10), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    name = db.Column(db.String(120), nullable=False)
    income_start_day = db.Column(db.Integer, nullable=False, default=1)

    household = db.relationship('Household', back_populates='pots')
    owner = db.relationship('User', foreign_keys=[owner_id])
    categories = db.relationship('Category', back_populates='pot', lazy='dynamic')
    bank_accounts = db.relationship('BankAccount', back_populates='pot', lazy='dynamic')

    @property
    def is_common(self):
        return self.kind == 'common'
