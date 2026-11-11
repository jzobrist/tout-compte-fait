from datetime import date
from app.extensions import db

class Income(db.Model):
    __tablename__ = 'income'
    __table_args__ = (db.UniqueConstraint('operation_id', name='uq_income_operation_id'),)

    id = db.Column(db.Integer, primary_key=True)
    pot_id = db.Column(db.Integer, db.ForeignKey('pot.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    label = db.Column(db.String(200), nullable=True)
    operation_id = db.Column(
        db.Integer, db.ForeignKey('operation.id', name='fk_income_operation_id'),
        nullable=True,
    )

    user = db.relationship('User', back_populates='incomes')
    pot = db.relationship('Pot')
    operation = db.relationship('Operation', back_populates='income')

