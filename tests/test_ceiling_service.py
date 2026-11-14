from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    BudgetMonthCategory, Category, EnvelopeCeiling, EnvelopePlan, Operation, RefloatPlan,
)
from app.services import ceiling_service as cs
from app.services import refloat_service as rs
from app.services import category_service
from app.services.balance_service import close_envelope
from app.services.budget_service import get_or_create_budget_month, recalculate_budget_month

TODAY = date(2026, 11, 15)


def _cat(db, pot, name, pct=None, floor=None):
    c = Category(name=name, type='dynamic', percentage=pct, min_amount=floor, pot_id=pot.id,
                 carry_over=False)
    db.session.add(c)
    db.session.commit()
    return c


def _bm(pot, income, year=2026, month=11):
    bm = get_or_create_budget_month(pot.id, year, month)
    bm.total_income = income
    recalculate_budget_month(bm)
    return bm


def _plan(db, pot, deficit, months=1, split=0.5):
    plan = RefloatPlan(pot_id=pot.id, year=2026, month=11, deficit=deficit, months=months, split=split)
    db.session.add(plan)
    db.session.commit()
    return plan


def _spend(db, h, pot, cat, amount, ref='R'):
    db.session.add(Operation(pot_id=pot.id, user_id=h.user_a.id, category_id=cat.id, amount=amount,
                             date=date(2026, 11, 5), direction='debit', classification='category',
                             bank_reference=ref))
    db.session.commit()


def _base(bm, cat):
    return BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=cat.id).one()


@pytest.fixture
def setup(db, household_clean):
    h = household_clean
    pot = h.personal_a
    a = _cat(db, pot, 'A', pct=0.2, floor=50.0)
    b = _cat(db, pot, 'B', pct=0.3)
    bm = _bm(pot, 1000.0)
    return h, pot, a, b, bm


def test_preset_is_percentage_without_deficit(setup):
    h, pot, a, b, bm = setup
    r = cs.report(bm)
    assert r.envelopes[a.id].amount == pytest.approx(200.0)
    assert r.envelopes[b.id].amount == pytest.approx(300.0)
    assert r.envelopes[a.id].auto and r.envelopes[b.id].auto
    assert r.unallocated == pytest.approx(500.0)
    assert not r.frozen


def test_preset_subtracts_refloat_auto_cuts(db, setup):
    h, pot, a, b, bm = setup
    _plan(db, pot, 100.0)
    r = cs.report(bm)
    assert r.envelopes[a.id].amount == pytest.approx(160.0)
    assert r.envelopes[b.id].amount == pytest.approx(240.0)
    assert r.unallocated == pytest.approx(1000 - 100 - 400)


def test_set_ceiling_clamped_and_stored(db, setup):
    h, pot, a, b, bm = setup
    cs.set_ceiling(pot, 2026, 11, a, 10.0)
    assert cs.report(bm).envelopes[a.id].amount == pytest.approx(50.0)
    cs.set_ceiling(pot, 2026, 11, a, 9999.0)
    r = cs.report(bm)
    assert r.envelopes[a.id].amount == pytest.approx(r.envelopes[a.id].high)
    assert r.envelopes[a.id].amount > 200.0
    assert not r.envelopes[a.id].auto
    assert EnvelopeCeiling.query.filter_by(category_id=a.id).count() == 1
    bmc = _base(bm, a)
    assert bmc.base_allocated == pytest.approx(r.envelopes[a.id].amount + bmc.carry_in)


def test_low_bound_is_spent(db, setup):
    h, pot, a, b, bm = setup
    _spend(db, h, pot, a, 120.0)
    assert cs.report(bm).envelopes[a.id].low == pytest.approx(120.0)
    cs.set_ceiling(pot, 2026, 11, a, 60.0)
    assert cs.report(bm).envelopes[a.id].amount == pytest.approx(120.0)


def test_personal_high_limited_by_unallocated(db, setup):
    h, pot, a, b, bm = setup
    unallocated = cs.report(bm).unallocated
    cs.set_ceiling(pot, 2026, 11, b, 100000.0)
    r = cs.report(bm)
    assert r.envelopes[b.id].amount == pytest.approx(300.0 + unallocated)
    assert r.unallocated == pytest.approx(0.0)


def test_closed_envelope_locked(db, setup):
    h, pot, a, b, bm = setup
    close_envelope(bm, a, TODAY)
    assert cs.report(bm).envelopes[a.id].locked
    with pytest.raises(cs.CeilingError, match='locked'):
        cs.set_ceiling(pot, 2026, 11, a, 100.0)


def test_frozen_month_refuses(db, setup):
    from datetime import datetime
    h, pot, a, b, bm = setup
    cs.set_ceiling(pot, 2026, 11, a, 100.0)
    cs.get_plan(pot, 2026, 11).validated_at = datetime(2026, 11, 15)
    db.session.commit()
    assert cs.is_frozen(pot, 2026, 11)
    with pytest.raises(cs.CeilingError, match='frozen'):
        cs.set_ceiling(pot, 2026, 11, a, 120.0)
    with pytest.raises(cs.CeilingError, match='frozen'):
        cs.reset(pot, 2026, 11)
    plan = _plan(db, pot, 50.0)
    with pytest.raises(cs.CeilingError, match='frozen'):
        rs.set_settings(plan, 2, None)


def test_set_ceiling_rejects_non_dynamic(db, setup):
    h, pot, a, b, bm = setup
    fixed = Category(name='Loyer', type='fixed_amount', amount=10.0, pot_id=pot.id, carry_over=False)
    db.session.add(fixed)
    db.session.commit()
    with pytest.raises(ValueError):
        cs.set_ceiling(pot, 2026, 11, fixed, 5.0)


def test_reset_by_settings(db, setup):
    h, pot, a, b, bm = setup
    cs.set_ceiling(pot, 2026, 11, a, 80.0)
    assert not cs.report(bm).envelopes[a.id].auto
    plan = _plan(db, pot, 100.0)
    rs.set_settings(plan, 2, None)
    r = cs.report(bm)
    assert r.envelopes[a.id].auto
    assert r.envelopes[a.id].amount == pytest.approx(180.0)  # 200 - 20 (share 50 prorata 40%)


def test_untouched_slider_follows_income_before_validation(db, setup):
    h, pot, a, b, bm = setup
    cs.set_ceiling(pot, 2026, 11, b, 250.0)
    bm.total_income = 2000.0
    recalculate_budget_month(bm)
    r = cs.report(bm)
    assert r.envelopes[a.id].amount == pytest.approx(400.0)
    assert r.envelopes[b.id].amount == pytest.approx(250.0)


def test_deleting_category_removes_stored_ceiling(db, setup):
    h, pot, a, b, bm = setup
    cs.set_ceiling(pot, 2026, 11, a, 80.0)
    assert EnvelopeCeiling.query.filter_by(category_id=a.id).count() == 1
    category_service.delete_family(h.user_a, a)
    db.session.commit()
    assert EnvelopeCeiling.query.count() == 0


def test_ceiling_rows_unique_and_deleted_with_category(db, household_clean):
    h = household_clean
    pot = h.personal_a
    cat = Category(name='Courses', type='dynamic', percentage=0.5, pot_id=pot.id, carry_over=False)
    db.session.add(cat)
    db.session.commit()
    plan = EnvelopePlan(pot_id=pot.id, year=2026, month=11)
    db.session.add(plan)
    db.session.commit()
    db.session.add(EnvelopeCeiling(plan_id=plan.id, category_id=cat.id, amount=120.0))
    db.session.commit()
    assert plan.validated_at is None
    assert [c.amount for c in plan.ceilings] == [120.0]

    db.session.add(EnvelopeCeiling(plan_id=plan.id, category_id=cat.id, amount=50.0))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()

    db.session.delete(cat)
    db.session.commit()
    assert EnvelopeCeiling.query.count() == 0
    assert EnvelopePlan.query.count() == 1
