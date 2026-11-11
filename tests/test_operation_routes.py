from app.models import Pot
import pytest

@pytest.fixture
def auth_client(client, db):
    client.post('/register', data={
        'email': 'ops@test.com', 'display_name': 'Ops',
        'password': 'Password1!', 'household_name': 'OpsFoyer',
    }, follow_redirects=True)
    return client

def test_add_operation(auth_client, db):
    from app.models import Category, User
    u = User.query.filter_by(email='ops@test.com').first()
    cat = Category.query.join(Pot).filter(Pot.household_id == u.household_id).first()
    resp = auth_client.post('/operations', data={
        'amount': '45.50',
        'category_id': str(cat.id),
        'label': 'Plein carburant',
        'date': '2026-05-20',
    }, follow_redirects=True)
    assert resp.status_code == 200
    from app.models.operation import Operation
    op = Operation.query.filter_by(label='Plein carburant').first()
    assert op is not None
    assert abs(op.amount - 45.50) < 0.01

def test_add_operation_with_specific_date(auth_client, db):
    from app.models import Category, User
    from app.models.operation import Operation
    u = User.query.filter_by(email='ops@test.com').first()
    cat = Category.query.join(Pot).filter(Pot.household_id == u.household_id).first()
    resp = auth_client.post('/operations', data={
        'amount': '22.00',
        'category_id': str(cat.id),
        'label': 'Test date',
        'date': '2026-05-15',
    }, follow_redirects=True)
    assert resp.status_code == 200
    op = Operation.query.filter_by(label='Test date').first()
    assert op is not None
    import datetime
    assert op.date == datetime.date(2026, 5, 15)

def test_delete_operation(auth_client, db):
    from app.models import Category, User
    from app.models.operation import Operation
    from datetime import date
    from app.extensions import db as _db
    u = User.query.filter_by(email='ops@test.com').first()
    cat = Category.query.join(Pot).filter(Pot.household_id == u.household_id).first()
    op = Operation(pot_id=cat.pot_id, user_id=u.id,
                   category_id=cat.id, amount=10.0, date=date.today(), label='Del me')
    _db.session.add(op)
    _db.session.commit()
    resp = auth_client.post(f'/operations/{op.id}/delete', follow_redirects=True)
    assert resp.status_code == 200
    from app.extensions import db as _db2
    assert _db2.session.get(Operation, op.id) is None
