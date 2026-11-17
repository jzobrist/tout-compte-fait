from datetime import date, datetime

import pytest

from app.models import (
    BankAccount, BudgetMonthCategory, Category, Operation, RefloatPlan, User,
)
from app.services import refloat_service as rs
from app.services.balance_service import close_envelope, free_money
from app.services.budget_service import get_or_create_budget_month, recalculate_budget_month
from app.services.pointage_service import classify

TODAY = date(2026, 11, 15)


def _account(db, pot, value, balance_at=datetime(2026, 11, 15, 6, 0), name='Compte'):
    acc = BankAccount(pot_id=pot.id, name=name, bank_name='Banque', balance_booked=value,
                      balance_value=value, balance_at=balance_at)
    db.session.add(acc)
    db.session.commit()
    return acc


def _cat(db, pot, name, type_='dynamic', pct=None, floor=None, amount=None):
    c = Category(name=name, type=type_, percentage=pct, min_amount=floor, amount=amount,
                 pot_id=pot.id, carry_over=False)
    db.session.add(c)
    db.session.commit()
    return c


def _dyn3(db, pot):
    return (_cat(db, pot, 'Courses', pct=0.6, floor=400.0), _cat(db, pot, 'Culture', pct=0.2),
            _cat(db, pot, 'Sorties', pct=0.2))


def _bm(pot, income, year=2026, month=11):
    bm = get_or_create_budget_month(pot.id, year, month)
    bm.total_income = income
    recalculate_budget_month(bm)
    return bm


def _plan(db, pot, deficit, months=1, split=0.5, year=2026, month=11):
    plan = RefloatPlan(pot_id=pot.id, year=year, month=month, deficit=deficit, months=months, split=split)
    db.session.add(plan)
    db.session.commit()
    return plan


def _common_plan(db, h, year=2026, month=11):
    """The common plan of the month: a personal pot of a 2-member household waits for it."""
    from app.models import ContributionPlan
    db.session.add(ContributionPlan(pot_id=h.common.id, year=year, month=month, position=0.0,
                                    created_at=datetime(2026, 11, 1)))
    db.session.commit()


def _base(bm, cat):
    return BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=cat.id).one().base_allocated


def _spend(db, h, pot, cat, amount, ref='R'):
    db.session.add(Operation(pot_id=pot.id, user_id=h.user_a.id, category_id=cat.id, amount=amount,
                             date=date(2026, 11, 5), direction='debit', classification='category',
                             bank_reference=ref))
    db.session.commit()


def test_ensure_plan_measures_deficit_from_free_money(db, household_clean):
    h = household_clean
    _common_plan(db, h)
    _account(db, h.personal_a, 100.0)
    _cat(db, h.personal_a, 'Loyer', type_='fixed_amount', amount=600.0)
    bm = _bm(h.personal_a, 1000.0)
    plan = rs.ensure_plan(h.personal_a, bm, TODAY)
    assert plan.deficit == pytest.approx(500.0)
    assert (plan.months, plan.split) == (1, 0.5)
    assert rs.ensure_plan(h.personal_a, bm, TODAY).id == plan.id


def test_ensure_plan_waits_for_income_and_fresh_balance(db, household_clean):
    h = household_clean
    acc = _account(db, h.personal_a, 100.0)
    bm = _bm(h.personal_a, 0.0)
    assert rs.ensure_plan(h.personal_a, bm, TODAY) is None
    bm.total_income = 1000.0
    acc.balance_at = datetime(2026, 10, 30, 6, 0)
    db.session.commit()
    assert rs.ensure_plan(h.personal_a, bm, TODAY) is None
    assert RefloatPlan.query.count() == 0


def test_deficit_frozen_after_close(db, household_clean):
    h = household_clean
    _common_plan(db, h)
    _account(db, h.personal_a, 100.0)
    rent = _cat(db, h.personal_a, 'Loyer', type_='fixed_amount', amount=600.0)
    bm = _bm(h.personal_a, 1000.0)
    plan = rs.ensure_plan(h.personal_a, bm, TODAY)
    close_envelope(bm, rent, TODAY)
    assert rs.ensure_plan(h.personal_a, bm, TODAY).deficit == pytest.approx(500.0)


def test_plan_defaults_from_previous_month(db, household_clean):
    h = household_clean
    _common_plan(db, h)
    _account(db, h.personal_a, 100.0)
    _plan(db, h.personal_a, 100.0, months=3, split=0.7, month=10)
    bm = _bm(h.personal_a, 1000.0)
    plan = rs.ensure_plan(h.personal_a, bm, TODAY)
    assert (plan.months, plan.split) == (2, 0.7)


def test_personal_cuts_prorata_with_floor(db, household_clean):
    h = household_clean
    pot = h.personal_a
    courses, culture, sorties = _dyn3(db, pot)
    bm = _bm(pot, 1000.0)
    plan = _plan(db, pot, 300.0)
    report = rs.envelope_cuts(plan, bm)
    assert report.cuts == pytest.approx({courses.id: 180.0, culture.id: 60.0, sorties.id: 60.0})
    assert report.unabsorbed == pytest.approx(0.0)
    recalculate_budget_month(bm)
    assert _base(bm, courses) == pytest.approx(420.0)
    plan.deficit = 600.0
    db.session.commit()
    report = rs.envelope_cuts(plan, bm)
    assert report.cuts == pytest.approx({courses.id: 200.0, culture.id: 120.0, sorties.id: 120.0})
    assert report.unabsorbed == pytest.approx(160.0)


def test_capacity_limited_by_remaining(db, household_clean):
    h = household_clean
    pot = h.personal_a
    courses, culture, sorties = _dyn3(db, pot)
    bm = _bm(pot, 1000.0)
    _spend(db, h, pot, culture, 180.0)
    plan = _plan(db, pot, 300.0)
    report = rs.envelope_cuts(plan, bm)
    assert report.capacity[culture.id] == pytest.approx(20.0)
    assert report.cuts[culture.id] == pytest.approx(20.0)


def test_cuts_without_dynamic_categories(db, household_clean):
    h = household_clean
    pot = h.personal_a
    _cat(db, pot, 'Loyer', type_='fixed_amount', amount=500.0)
    bm = _bm(pot, 1000.0)
    plan = _plan(db, pot, 300.0)
    report = rs.envelope_cuts(plan, bm)
    assert report.cuts == {}
    assert report.unabsorbed == pytest.approx(300.0)
    recalculate_budget_month(bm)


def test_settings_clamp_months(db, household_clean):
    h = household_clean
    pot = h.personal_a
    _dyn3(db, pot)
    _bm(pot, 1000.0)
    plan = _plan(db, pot, 300.0)
    rs.set_settings(plan, 2, None)
    assert plan.months == 2
    rs.set_settings(plan, 99, None)
    assert plan.months == 6


def _common_with_incomes(db, h, income_a=3000.0, income_b=2000.0):
    _bm(h.personal_a, income_a)
    _bm(h.personal_b, income_b)
    return _bm(h.common, 1000.0)


def test_recalculate_idempotent_with_plan(db, household_clean):
    h = household_clean
    pot = h.personal_a
    courses, culture, sorties = _dyn3(db, pot)
    bm = _bm(pot, 1000.0)
    _plan(db, pot, 300.0)
    for _ in range(3):
        recalculate_budget_month(bm)
    assert [_base(bm, c) for c in (courses, culture, sorties)] == pytest.approx([420.0, 140.0, 140.0])


# --- final review fixes ---

def test_old_pending_entries_not_subtracted(db, household_clean):
    h = household_clean
    _account(db, h.common, 1000.0)
    rent = _cat(db, h.common, 'Loyer', type_='fixed_amount', amount=100.0)
    bm = _bm(h.common, 1000.0)
    db.session.add(Operation(pot_id=h.common.id, user_id=h.user_a.id, category_id=rent.id,
                             amount=40.0, date=date(2026, 8, 1), direction='debit',
                             classification='category'))
    db.session.commit()
    fm = free_money(h.common, bm)
    assert fm.pending_entries == 0.0
    assert fm.value == pytest.approx(900.0)


def test_envelope_part_common_uses_split_only(db, household_clean):
    h = household_clean
    _common_with_incomes(db, h, 0.0, 0.0)  # no personal income: J4 capped members to 0
    common_plan = _plan(db, h.common, 600.0, months=2, split=0.25)
    assert rs.envelope_part(common_plan) == pytest.approx(225.0)
    personal_plan = _plan(db, h.personal_a, 600.0, months=2)
    assert rs.envelope_part(personal_plan) == pytest.approx(300.0)


def test_personal_plan_waits_for_the_common_plan_of_the_month(db, household_clean):
    """Without the common plan the transfer to the common pot is unknown: no measure yet."""
    h = household_clean
    _account(db, h.personal_a, 100.0)
    _cat(db, h.personal_a, 'Loyer', type_='fixed_amount', amount=600.0)
    bm = _bm(h.personal_a, 1000.0)
    assert rs.ensure_plan(h.personal_a, bm, TODAY) is None
    assert RefloatPlan.query.count() == 0
    _common_plan(db, h)
    assert rs.ensure_plan(h.personal_a, bm, TODAY).deficit == pytest.approx(500.0)


def test_personal_plan_measures_the_planned_transfer(db, household_clean):
    """The deficit counts the transfer the common plan asks for, not the category's base amount."""
    from app.models import Category
    from tests.test_advance import _ref
    from tests.test_contribution_service import _account as _c_account
    h = household_clean
    Category.query.filter_by(pot_id=h.personal_a.id, role='common_transfer').delete()
    db.session.add(Category(name='Virement vers le commun', type='fixed_amount', amount=0.0,
                            pot_id=h.personal_a.id, role='common_transfer', carry_over=False))
    db.session.commit()
    _ref(db, h)                                  # November: A transfers 1100 + pays the Prêt 800
    _c_account(db, h.personal_a, 600.0)
    bm = get_or_create_budget_month(h.personal_a.id, 2026, 11)
    plan = rs.ensure_plan(h.personal_a, bm, TODAY)
    fm = free_money(h.personal_a, bm)
    assert fm.remaining >= 1100.0 + 800.0                   # planned transfer reserved, not 0 €
    assert plan.deficit == pytest.approx(max(0.0, -fm.value))
    assert plan.deficit > 0
