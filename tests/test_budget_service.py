import pytest
from datetime import date
from app.models import User, Category, Pot
from app.services.household_service import create_household
from app.models.budget import BudgetMonth, BudgetMonthCategory
from app.services.budget_service import (
    target_budget_month,
    get_or_create_budget_month,
    recalculate_budget_month,
    get_category_effective_budget,
    get_spent_for_category,
    compute_daily_metrics,
)

@pytest.fixture
def household_with_categories(db):
    u = User(email='svc@test.com', display_name='Svc')
    u.set_password('x')
    h = create_household('Svc Test', u)
    pot = Pot.query.filter_by(household_id=h.id, kind='common').one()
    # fixed_auto: loyer 800
    loyer = Category(name='Loyer test', type='fixed_auto', amount=800.0, auto_day=1,
                     pot_id=pot.id, carry_over=False)
    # fixed_amount: courses 400
    courses = Category(name='Courses test', type='fixed_amount', amount=400.0,
                       pot_id=pot.id, carry_over=True)
    # dynamic: loisirs 15% min=50 max=300
    loisirs = Category(name='Loisirs test', type='dynamic', percentage=0.15,
                       min_amount=50.0, max_amount=300.0,
                       pot_id=pot.id, carry_over=True)
    db.session.add_all([loyer, courses, loisirs])
    db.session.commit()
    return pot, u, loyer, courses, loisirs

def test_get_or_create_budget_month(db, household_with_categories):
    h, u, *_ = household_with_categories  # h is the pot
    bm = get_or_create_budget_month(h.id, 2026, 5)
    assert bm.id is not None
    assert bm.year == 2026 and bm.month == 5
    # idempotent
    bm2 = get_or_create_budget_month(h.id, 2026, 5)
    assert bm2.id == bm.id

def test_recalculate_with_income(db, household_with_categories):
    h, u, loyer, courses, loisirs = household_with_categories
    bm = get_or_create_budget_month(h.id, 2026, 5)
    bm.total_income = 3000.0
    db.session.commit()
    recalculate_budget_month(bm)
    # disponible = 3000 - 800 (fixed_auto) - 400 (fixed_amount) = 1800
    # loisirs = max(50, min(300, 1800 * 0.15)) = max(50, min(300, 270)) = 270
    loisirs_bmc = BudgetMonthCategory.query.filter_by(
        budget_month_id=bm.id, category_id=loisirs.id).first()
    assert loisirs_bmc is not None
    assert abs(loisirs_bmc.base_allocated - 270.0) < 0.01

def test_get_category_effective_budget(db, household_with_categories):
    h, u, loyer, courses, loisirs = household_with_categories
    bm = get_or_create_budget_month(h.id, 2026, 5)
    bm.total_income = 3000.0
    db.session.commit()
    recalculate_budget_month(bm)
    effective = get_category_effective_budget(bm, loisirs)
    assert abs(effective - 270.0) < 0.01

def test_compute_daily_metrics(db, household_with_categories):
    h, u, loyer, courses, loisirs = household_with_categories
    bm = get_or_create_budget_month(h.id, 2026, 5)
    bm.total_income = 3000.0
    db.session.commit()
    recalculate_budget_month(bm)
    metrics = compute_daily_metrics(bm, loisirs, current_day=10, days_in_month=31, spent=50.0)
    assert 'ideal_daily' in metrics
    assert 'ideal_cumulated' in metrics
    assert 'remaining_daily' in metrics
    assert metrics['ideal_cumulated'] == pytest.approx(270.0 * 10 / 31, rel=1e-3)


def test_budget_isolated_between_pots(db, household_ab):
    h = household_ab
    bm_common = get_or_create_budget_month(h.common.id, 2026, 11)
    bm_perso = get_or_create_budget_month(h.personal_a.id, 2026, 11)
    assert bm_common.id != bm_perso.id
    bm_perso.total_income = 1000.0
    db.session.commit()
    recalculate_budget_month(bm_perso)
    recalculate_budget_month(bm_common)
    loisirs = Category.query.filter_by(pot_id=h.personal_a.id, name='Loisirs perso').one()
    bmc = BudgetMonthCategory.query.filter_by(
        budget_month_id=bm_perso.id, category_id=loisirs.id).one()
    assert bmc.base_allocated == pytest.approx(150.0)
    assert bmc.carry_in == 0
    assert bm_common.total_income == 0
    common_dynamic = Category.query.filter_by(pot_id=h.common.id, type='dynamic').all()
    for cat in common_dynamic:
        c = BudgetMonthCategory.query.filter_by(
            budget_month_id=bm_common.id, category_id=cat.id).one()
        assert c.base_allocated == pytest.approx(cat.min_amount or 0.0)
    assert BudgetMonthCategory.query.filter_by(
        budget_month_id=bm_perso.id, category_id=common_dynamic[0].id).first() is None


def test_income_start_day_per_pot(db, household_ab):
    h = household_ab
    h.common.income_start_day = 25
    h.personal_a.income_start_day = 1
    db.session.commit()
    assert target_budget_month(date(2026, 11, 27), h.common.income_start_day) == (2026, 12)
    assert target_budget_month(date(2026, 11, 27), h.personal_a.income_start_day) == (2026, 11)
