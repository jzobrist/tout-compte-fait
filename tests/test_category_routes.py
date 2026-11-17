from app.models import Pot
import pytest

@pytest.fixture
def auth_client(client, db):
    client.post('/register', data={
        'email': 'cat@test.com', 'display_name': 'Cat',
        'password': 'Password1!', 'household_name': 'CatFoyer',
    }, follow_redirects=True)
    return client

def test_categories_page_loads(auth_client, db):
    resp = auth_client.get('/categories')
    assert resp.status_code == 200

def test_create_dynamic_category(auth_client, db):
    from app.models import User, Category
    from app.models import Pot
    pot = Pot.query.filter_by(kind='common').one()
    resp = auth_client.post('/categories', data={
        'name': 'Hobbies',
        'type': 'dynamic',
        'common': 'y',
        'common-percentage': '8',
        'common-min_amount': '40',
        'common-carry_over': 'y',
    }, follow_redirects=True)
    assert resp.status_code == 200
    u = User.query.filter_by(email='cat@test.com').first()
    cat = Category.query.join(Pot).filter(Pot.household_id == u.household_id, Category.name == 'Hobbies').first()
    assert cat is not None
    assert abs(cat.percentage - 0.08) < 0.001

def test_edit_category_does_not_duplicate(auth_client, db):
    """Editing a category must not create a new row."""
    from app.models import User, Category
    u = User.query.filter_by(email='cat@test.com').first()
    # Get the seeded Loyer category
    cat = Category.query.filter_by(
        name='Loyer / Prêt immobilier'
    ).first()
    count_before = Category.query.join(Pot).filter(Pot.household_id == u.household_id).count()
    resp = auth_client.post(f'/categories/{cat.id}/edit', data={
        'name': 'Loyer / Prêt immobilier',
        'type': 'fixed_auto',
        'common': 'y',
        'personal': '',
        'common-amount': '850',
        'common-carry_over': 'y',
    }, follow_redirects=True)
    assert resp.status_code == 200
    count_after = Category.query.join(Pot).filter(Pot.household_id == u.household_id).count()
    assert count_after == count_before, "Edit must not create a new category"
    cat_reloaded = db.session.get(Category, cat.id)
    assert abs(cat_reloaded.amount - 850) < 0.01


def test_edit_page_is_a_full_page_without_auto_day(household_ab):
    from app.models import Category
    cat = Category.query.filter_by(pot_id=household_ab.common.id).first()
    html = household_ab.client_a.get(f'/categories/{cat.id}/edit').get_data(as_text=True)
    assert '<nav' in html or 'navbar' in html  # rendered inside base.html
    assert 'Jour de débit auto' not in html
    assert 'Charge fixe (prélèvement)' in html
    fragment = household_ab.client_a.get(f'/categories/{cat.id}/edit',
                                         headers={'HX-Request': 'true'}).get_data(as_text=True)
    assert 'navbar' not in fragment and '<form' in fragment


def test_spending_on_zero_budget_shows_full_danger_gauge(db, household_ab):
    from datetime import date
    from app.models import Category, Operation
    h = household_ab
    cat = Category.query.filter_by(pot_id=h.common.id, name='Loyer / Prêt immobilier').one()
    db.session.add(Operation(pot_id=h.common.id, user_id=h.user_a.id, category_id=cat.id,
                             amount=1068.09, date=date.today(), direction='debit',
                             classification='category', bank_reference='R1'))
    db.session.commit()
    html = h.client_a.get('/').get_data(as_text=True)
    assert 'cat-progress-bar danger" style="width: 100%;"' in html


def test_percentage_is_typed_in_percent(auth_client, db):
    from app.models import Category
    auth_client.post('/categories', data={
        'name': 'Sorties', 'type': 'dynamic', 'common': 'y',
        'common-percentage': '12,5', 'common-min_amount': '30'})
    cat = Category.query.filter_by(name='Sorties').one()
    assert abs(cat.percentage - 0.125) < 1e-9
    html = auth_client.get(f'/categories/{cat.id}/edit').get_data(as_text=True)
    assert 'name="common-percentage"' in html and 'value="12.5"' in html
    assert 'value="30"' in html and 'value="30.0"' not in html
    assert 'Pourcentage du disponible (%)' in html


def test_fixed_only_fields_hidden_with_d_none(auth_client, db):
    """Bootstrap d-flex is !important: the type toggle must use the d-none class, not style.display."""
    html = auth_client.get('/categories').get_data(as_text=True)
    assert "classList.toggle('d-none'" in html
    assert 'style.display' not in html
