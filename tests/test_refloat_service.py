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


def _base(bm, cat):
    return BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=cat.id).one().base_allocated


def _spend(db, h, pot, cat, amount, ref='R'):
    db.session.add(Operation(pot_id=pot.id, user_id=h.user_a.id, category_id=cat.id, amount=amount,
                             date=date(2026, 11, 5), direction='debit', classification='category',
                             bank_reference=ref))
    db.session.commit()


def test_ensure_plan_measures_deficit_from_free_money(db, household_clean):
    h = household_clean
    _account(db, h.common, 100.0)
    _cat(db, h.common, 'Loyer', type_='fixed_amount', amount=600.0)
    bm = _bm(h.common, 1000.0)
    plan = rs.ensure_plan(h.common, bm, TODAY)
    assert plan.deficit == pytest.approx(500.0)
    assert (plan.months, plan.split) == (1, 0.5)
    assert rs.ensure_plan(h.common, bm, TODAY).id == plan.id


def test_ensure_plan_waits_for_income_and_fresh_balance(db, household_clean):
    h = household_clean
    acc = _account(db, h.common, 100.0)
    bm = _bm(h.common, 0.0)
    assert rs.ensure_plan(h.common, bm, TODAY) is None
    bm.total_income = 1000.0
    acc.balance_at = datetime(2026, 10, 30, 6, 0)
    db.session.commit()
    assert rs.ensure_plan(h.common, bm, TODAY) is None
    assert RefloatPlan.query.count() == 0


def test_deficit_frozen_after_close(db, household_clean):
    h = household_clean
    _account(db, h.common, 100.0)
    rent = _cat(db, h.common, 'Loyer', type_='fixed_amount', amount=600.0)
    bm = _bm(h.common, 1000.0)
    plan = rs.ensure_plan(h.common, bm, TODAY)
    close_envelope(bm, rent, TODAY)
    assert rs.ensure_plan(h.common, bm, TODAY).deficit == pytest.approx(500.0)


def test_plan_defaults_from_previous_month(db, household_clean):
    h = household_clean
    _account(db, h.common, 100.0)
    _plan(db, h.common, 100.0, months=3, split=0.7, month=10)
    bm = _bm(h.common, 1000.0)
    plan = rs.ensure_plan(h.common, bm, TODAY)
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


def test_member_transfers_prorata_and_cap(db, household_clean):
    h = household_clean
    _common_with_incomes(db, h)
    plan = _plan(db, h.common, 300.0, split=1.0)
    by_user = {t.user.id: t for t in rs.member_transfers(plan)}
    assert by_user[h.user_a.id].requested == pytest.approx(180.0)
    assert by_user[h.user_b.id].requested == pytest.approx(120.0)
    assert not by_user[h.user_b.id].capped
    assert rs.envelope_part(plan) == pytest.approx(0.0)
    _cat(db, h.personal_b, 'Loyer B', type_='fixed_amount', amount=1950.0)
    by_user = {t.user.id: t for t in rs.member_transfers(plan)}
    assert by_user[h.user_b.id].requested == pytest.approx(50.0)
    assert by_user[h.user_b.id].capped
    assert by_user[h.user_a.id].requested == pytest.approx(180.0)
    assert rs.envelope_part(plan) == pytest.approx(70.0)


def test_single_member_gets_whole_transfer(db):
    from app.models import Pot
    from app.services.household_service import create_household
    u = User(email='solo@test.com', display_name='Solo')
    u.set_password('x')
    hh = create_household('Solo', u)
    Category.query.delete()
    db.session.commit()
    common = Pot.query.filter_by(household_id=hh.id, kind='common').one()
    personal = Pot.query.filter_by(owner_id=u.id).one()
    _bm(personal, 1000.0)
    _bm(common, 500.0)
    plan = _plan(db, common, 300.0, split=1.0)
    [t] = rs.member_transfers(plan)
    assert t.requested == pytest.approx(300.0)


def test_no_income_members_capped_to_envelopes(db, household_clean):
    h = household_clean
    _common_with_incomes(db, h, 0.0, 0.0)
    plan = _plan(db, h.common, 300.0, split=1.0)
    transfers = rs.member_transfers(plan)
    assert all(t.capped and t.requested == 0.0 for t in transfers)
    assert rs.envelope_part(plan) == pytest.approx(300.0)


def test_supplement_reduces_personal_dynamic_envelopes(db, household_clean):
    h = household_clean
    loisirs = _cat(db, h.personal_a, 'Loisirs', pct=0.5)
    pbm = _bm(h.personal_a, 3000.0)
    _bm(h.personal_b, 2000.0)
    _bm(h.common, 1000.0)
    assert _base(pbm, loisirs) == pytest.approx(1500.0)
    plan = _plan(db, h.common, 300.0, split=1.0)
    rs.set_settings(plan, 1, 1.0)
    assert rs.supplement_requested_for(h.personal_a, 2026, 11) == pytest.approx(180.0)
    assert _base(pbm, loisirs) == pytest.approx((3000.0 - 180.0) * 0.5)


def test_contribution_does_not_inflate_common(db, household_clean):
    h = household_clean
    courses, culture, sorties = _dyn3(db, h.common)
    _account(db, h.personal_b, 500.0, name='Perso B')
    pbm_b = get_or_create_budget_month(h.personal_b.id, 2026, 11)
    common_acc = _account(db, h.common, 0.0, name='Joint')
    cbm = _common_with_incomes(db, h)
    plan = _plan(db, h.common, 300.0, split=1.0)
    recalculate_budget_month(cbm)
    before = _base(cbm, courses)
    op = Operation(pot_id=h.common.id, bank_account_id=common_acc.id, user_id=h.user_a.id,
                   amount=100.0, date=date(2026, 11, 12), bank_date=date(2026, 11, 12),
                   direction='credit', bank_reference='VIR1', bank_label='VIR B')
    db.session.add(op)
    db.session.commit()
    classify(op, h.user_a, 'contribution', contributor=h.user_b)
    assert cbm.total_income == pytest.approx(1000.0)
    assert _base(cbm, courses) == pytest.approx(before)
    by_user = {t.user.id: t for t in rs.member_transfers(plan)}
    assert by_user[h.user_b.id].received == pytest.approx(100.0)
    assert by_user[h.user_b.id].due == pytest.approx(20.0)
    assert free_money(h.personal_b, pbm_b).transfer_due == pytest.approx(20.0)
    assert free_money(h.common, cbm).transfer_expected == pytest.approx(180.0 + 20.0)


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

def test_supplement_received_counted_in_common_budget_month(db, household_clean):
    from app.services.pointage_service import contribution_received
    h = household_clean
    h.common.income_start_day = 25
    db.session.commit()
    acc = _account(db, h.common, 0.0, name='Joint')
    op = Operation(pot_id=h.common.id, bank_account_id=acc.id, user_id=h.user_a.id, amount=100.0,
                   date=date(2026, 11, 27), bank_date=date(2026, 11, 27), direction='credit',
                   bank_reference='VIR27', bank_label='VIR B')
    db.session.add(op)
    db.session.commit()
    classify(op, h.user_a, 'contribution', contributor=h.user_b)
    assert contribution_received(h.common, h.user_b, 2026, 12) == pytest.approx(100.0)
    assert contribution_received(h.common, h.user_b, 2026, 11) == 0.0


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
