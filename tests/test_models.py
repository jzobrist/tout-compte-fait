from app.models import Pot, User, Household, Category, Income, BudgetMonth, BudgetMonthCategory, Operation, BudgetAdjustment, AlertLog
from datetime import date

def test_household_creation(db):
    h = Household(name='Famille Dupont')
    db.session.add(h)
    db.session.commit()
    assert h.id is not None

def test_user_creation(db):
    h = Household(name='Test Foyer')
    db.session.add(h)
    db.session.flush()
    u = User(email='test@example.com', display_name='Test', household_id=h.id)
    u.set_password('secret123')
    db.session.add(u)
    db.session.commit()
    assert u.id is not None
    assert u.check_password('secret123')
    assert not u.check_password('wrong')

def test_invite_code(db):
    h = Household(name='Invite Test')
    db.session.add(h)
    db.session.commit()
    h.generate_invite()
    db.session.commit()
    assert h.invite_code is not None
    assert h.invite_expires_at is not None
    assert h.is_invite_valid()

def test_category_types(db):
    h = Household(name='Cat Test')
    db.session.add(h)
    db.session.flush()
    pot = Pot(household_id=h.id, kind='common', name='Commun')
    db.session.add(pot)
    db.session.flush()
    c = Category(name='Loyer', type='fixed_auto', amount=800.0,
                 auto_day=1, carry_over=False, pot_id=pot.id)
    db.session.add(c)
    db.session.commit()
    assert c.id is not None
    assert c.type == 'fixed_auto'

def test_budget_month(db):
    h = Household(name='Budget Test')
    db.session.add(h)
    db.session.flush()
    pot = Pot(household_id=h.id, kind='common', name='Commun')
    db.session.add(pot)
    db.session.flush()
    bm = BudgetMonth(pot_id=pot.id, year=2026, month=5, total_income=3000.0)
    db.session.add(bm)
    db.session.commit()
    assert bm.id is not None

def test_operation(db):
    h = Household(name='Op Test')
    db.session.add(h)
    db.session.flush()
    u = User(email='op@test.com', display_name='Op', household_id=h.id)
    u.set_password('x')
    pot = Pot(household_id=h.id, kind='common', name='Commun')
    db.session.add_all([u, pot])
    db.session.flush()
    c = Category(name='Essence', type='fixed_amount', amount=100.0, pot_id=pot.id)
    db.session.add(c)
    db.session.flush()
    op = Operation(pot_id=pot.id, user_id=u.id, category_id=c.id,
                   amount=30.0, date=date.today(), label='Plein AP')
    db.session.add(op)
    db.session.commit()
    assert op.id is not None
