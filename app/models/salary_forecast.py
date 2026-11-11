from app.extensions import db


class SalaryForecast(db.Model):
    """Salary typed by `user` for `member` in the plan simulation (private to `user`)."""
    __tablename__ = 'salary_forecast'
    __table_args__ = (db.UniqueConstraint('user_id', 'member_id', name='uq_salary_forecast'),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    amount = db.Column(db.Double, nullable=False)
