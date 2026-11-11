import pytest
from app import create_app
from app.extensions import db as _db

@pytest.fixture(scope='session')
def app():
    app = create_app('testing')

    @app.teardown_request
    def _forget_login_user(exc):
        # (flask-login g leak) The session-scoped app context is reused by every request, so
        # flask-login's cached g._login_user would leak between test clients.
        from flask import g
        g.pop('_login_user', None)

    with app.app_context():
        yield app

@pytest.fixture(scope='function')
def db(app):
    with app.app_context():
        _db.create_all()
        yield _db
        _db.session.remove()
        _db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def household_ab(app, db):
    """Household with users A and B built through the real register/invite flow."""
    from types import SimpleNamespace
    from app.models import User, Pot

    client_a = app.test_client()
    client_b = app.test_client()
    client_a.post('/register', data={
        'email': 'a@test.com', 'display_name': 'A',
        'password': 'Password1!', 'household_name': 'Foyer',
    })
    client_a.post('/settings/invite')
    from app.models import Household
    household = Household.query.first()
    client_b.post(f'/invite/{household.invite_code}', data={
        'email': 'b@test.com', 'display_name': 'B', 'password': 'Password1!',
    })
    user_a = User.query.filter_by(email='a@test.com').one()
    user_b = User.query.filter_by(email='b@test.com').one()
    return SimpleNamespace(
        user_a=user_a, user_b=user_b, client_a=client_a, client_b=client_b,
        common=Pot.query.filter_by(household_id=household.id, kind='common').one(),
        personal_a=Pot.query.filter_by(owner_id=user_a.id).one(),
        personal_b=Pot.query.filter_by(owner_id=user_b.id).one(),
    )


@pytest.fixture
def household_clean(household_ab, db):
    """household_ab without the seeded default categories, so budget amounts are exact."""
    from app.models import Category
    Category.query.delete()
    db.session.commit()
    return household_ab


@pytest.fixture
def frozen_today(monkeypatch, request):
    """Pin 'today' to mid-month in every module the J6 flow reads it from."""
    import datetime as dt
    import types
    from app.routes import plan as plan_routes
    from app.services import alert_service, pointage_service

    class FakeDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 15)

    monkeypatch.setattr(plan_routes, 'date', FakeDate)
    monkeypatch.setattr(alert_service, 'date', FakeDate)
    monkeypatch.setattr(request.module, 'date', FakeDate, raising=False)
    fake_dt = types.SimpleNamespace(**{k: v for k, v in vars(dt).items() if not k.startswith('__')})
    fake_dt.date = FakeDate
    monkeypatch.setattr(pointage_service, 'datetime', fake_dt)
    return FakeDate.today()
