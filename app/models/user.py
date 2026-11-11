from flask_login import UserMixin
from app.extensions import db as _db, bcrypt, login_manager

class User(UserMixin, _db.Model):
    __tablename__ = 'user'

    id = _db.Column(_db.Integer, primary_key=True)
    email = _db.Column(_db.String(255), unique=True, nullable=False)
    display_name = _db.Column(_db.String(80), nullable=False)
    password_hash = _db.Column(_db.String(255), nullable=False)
    household_id = _db.Column(_db.Integer, _db.ForeignKey('household.id'), nullable=False)

    household = _db.relationship('Household', back_populates='users')
    operations = _db.relationship('Operation', back_populates='user', lazy='dynamic',
                                  foreign_keys='Operation.user_id')
    incomes = _db.relationship('Income', back_populates='user', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(user_id):
    return _db.session.get(User, int(user_id))
