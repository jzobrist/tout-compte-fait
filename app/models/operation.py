from datetime import date
from app.extensions import db

class Operation(db.Model):
    __tablename__ = 'operation'
    __table_args__ = (
        db.UniqueConstraint('bank_account_id', 'bank_reference', name='uq_operation_account_bank_ref'),
    )

    id = db.Column(db.Integer, primary_key=True)
    pot_id = db.Column(db.Integer, db.ForeignKey('pot.id'), nullable=False)
    bank_account_id = db.Column(db.Integer, db.ForeignKey('bank_account.id'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    amount = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    label = db.Column(db.String(200), nullable=True)
    auto_generated = db.Column(db.Boolean, default=False)
    bank_reference = db.Column(db.String(50), nullable=True)
    direction = db.Column(db.String(10), nullable=False, default='debit', server_default='debit')
    classification = db.Column(db.String(20), nullable=True)
    bank_label = db.Column(db.String(200), nullable=True)
    bank_date = db.Column(db.Date, nullable=True)
    entered_amount = db.Column(db.Float, nullable=True)
    # classification 'refloat_transfer': member whose transfer this credit is
    contributor_id = db.Column(
        db.Integer, db.ForeignKey('user.id', name='fk_operation_contributor_id'), nullable=True,
    )

    user = db.relationship('User', back_populates='operations', foreign_keys=[user_id])
    contributor = db.relationship('User', foreign_keys=[contributor_id])
    category = db.relationship('Category', back_populates='operations')
    pot = db.relationship('Pot')
    bank_account = db.relationship('BankAccount')
    income = db.relationship('Income', back_populates='operation', uselist=False)

    @property
    def is_bank_movement(self):
        return self.bank_reference is not None

    @property
    def is_pending(self):
        return self.bank_reference is None and self.classification == 'category'

    @property
    def is_to_classify(self):
        return self.bank_reference is not None and self.classification is None
