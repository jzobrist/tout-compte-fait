"""Common « Revenus » = money really received; contributions made before the first plan count."""
from datetime import date, datetime

import pytest

from app.extensions import db as _db
from app.models import BankAccount, Category, ContributionPlan, Operation
from app.services import contribution_service as cs
from app.services.pointage_service import contribution_received
from tests.test_advance import _ref
from tests.test_contribution_service import M, TODAY, _ready  # noqa: F401


def _acc(pot):
    acc = BankAccount.query.filter_by(pot_id=pot.id).first()
    if acc is None:
        acc = BankAccount(pot_id=pot.id, name='Perso', bank_name='B')
        _db.session.add(acc)
        _db.session.commit()
    return acc


_refs = iter(range(10_000))


def _contribution(h, user, amount, day):
    op = Operation(pot_id=h.common.id, user_id=user.id, amount=amount, date=day, label='VIR',
                   direction='credit', bank_account_id=_acc(h.common).id, bank_label='VIR',
                   bank_date=day, bank_reference=f'C{next(_refs)}', classification='contribution',
                   contributor_id=user.id)
    _db.session.add(op)
    _db.session.commit()
    return op


def _perso_debit(h, user_pot, user, category, amount, day):
    op = Operation(pot_id=h.common.id, user_id=user.id, amount=amount, date=day, label='CB',
                   direction='debit', bank_account_id=_acc(user_pot).id, bank_label='CB',
                   bank_date=day, bank_reference=f'D{next(_refs)}', classification='category',
                   category_id=category.id)
    _db.session.add(op)
    _db.session.commit()
    return op


# --- contributions made before the first plan --------------------------------------------------

def test_contribution_before_first_plan_counts_from_month_start(db, household_clean):
    h = household_clean
    h.common.income_start_day = 26          # November = 26/10 → 25/11
    _db.session.commit()
    plan = _ready(db, h)                    # prepared on 15/11, no October plan
    assert plan is not None and plan.created_at.date() > date(2026, 10, 29)
    _contribution(h, h.user_a, 2000.0, date(2026, 10, 29))
    _contribution(h, h.user_a, 50.0, date(2026, 10, 20))     # before the month: not counted
    assert contribution_received(h.common, h.user_a, *M) == pytest.approx(2000.0)


def test_contribution_before_plan_belongs_to_previous_plan_when_it_exists(db, household_clean):
    h = household_clean
    h.common.income_start_day = 26
    _db.session.add(ContributionPlan(pot_id=h.common.id, year=2026, month=10,
                                     created_at=datetime(2026, 9, 26, 8, 0)))
    _db.session.commit()
    plan = _ready(db, h)
    assert plan is not None
    _contribution(h, h.user_a, 2000.0, date(2026, 10, 29))
    assert contribution_received(h.common, h.user_a, *M) == 0.0
    assert contribution_received(h.common, h.user_a, 2026, 10) == pytest.approx(2000.0)


# --- common « Revenus » ------------------------------------------------------------------------

def test_common_income_before_any_transfer_is_all_expected(db, household_clean):
    h = household_clean
    _ref(db, h)                              # transfers A 1100, B 900, Prêt 800 paid by A
    inc = cs.common_income(h.common, *M)
    assert inc.received == 0.0
    assert inc.expected == pytest.approx(2800.0)


def test_common_income_counts_extra_transfer_and_charges_paid_from_perso(db, household_clean):
    h = household_clean
    _, pret = _ref(db, h)
    _contribution(h, h.user_a, 1500.0, date(2026, 11, 16))   # 400 € more than the transfer
    _perso_debit(h, h.personal_a, h.user_a, pret, 800.0, date(2026, 11, 5))
    inc = cs.common_income(h.common, *M)
    assert inc.received == pytest.approx(2300.0)              # 1500 + 800, nothing shared out
    assert inc.expected == pytest.approx(500.0)               # B's 900 − A's 400 extra


def test_common_income_without_plan(db, household_clean):
    h = household_clean
    inc = cs.common_income(h.common, *M)
    assert (inc.received, inc.expected) == (0.0, 0.0)


def test_dashboard_common_revenus_shows_received_and_expected(db, household_clean, monkeypatch):
    import app.routes.dashboard as dash

    class _D(date):
        @classmethod
        def today(cls):
            return TODAY
    monkeypatch.setattr(dash, 'date', _D)
    h = household_clean
    _ref(db, h)
    _contribution(h, h.user_b, 900.0, date(2026, 11, 16))
    h.client_a.post(f'/pots/{h.common.id}/select')
    html = h.client_a.get('/').get_data(as_text=True)
    assert '>900,00€</div>' in html                          # received: B's transfer
    assert '+ 1 900,00 € attendus' in html                   # A's transfer 1100 + Prêt 800


# --- position of a month whose previous month was never used ------------------------------------

def _october_with_envelopes(db, h):
    from app.services.budget_service import get_or_create_budget_month, recalculate_budget_month
    from tests.test_contribution_service import _ccat
    loyer = _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=1000.0)
    recalculate_budget_month(get_or_create_budget_month(h.common.id, 2026, 10))
    return loyer


def test_position_ignores_envelopes_of_an_unused_previous_month(db, household_clean):
    h = household_clean
    _october_with_envelopes(db, h)            # October created on the fly, nothing ever pointed
    plan = _ready(db, h, balance=500.0)
    assert plan.position == pytest.approx(500.0)


def _used_october(db, h, carry_over):
    loyer = _october_with_envelopes(db, h)
    loyer.carry_over = carry_over
    _db.session.add(Operation(pot_id=h.common.id, user_id=h.user_a.id, amount=200.0,
                              date=date(2026, 10, 10), label='LOYER', direction='debit',
                              bank_label='LOYER', bank_date=date(2026, 10, 10), bank_reference='L1',
                              classification='category', category_id=loyer.id))
    _db.session.commit()


def test_position_frees_uncarried_remaining_of_a_used_previous_month(db, household_clean):
    """October is over when November is prepared: its unspent 800 € without carry-over are free."""
    h = household_clean
    _used_october(db, h, carry_over=False)
    plan = _ready(db, h, balance=500.0)
    assert plan.position == pytest.approx(500.0)


def test_position_keeps_remaining_while_previous_month_still_runs(db, household_clean):
    """Simulation of December on 15/11: November is not over, its envelopes can still be spent."""
    from tests.test_contribution_service import _account, _ccat
    from app.services.budget_service import get_or_create_budget_month, recalculate_budget_month
    h = household_clean
    courses = _ccat(db, h.common, 'Courses', 'fixed_amount', amount=600.0)
    recalculate_budget_month(get_or_create_budget_month(h.common.id, 2026, 11))
    _db.session.add(Operation(pot_id=h.common.id, user_id=h.user_a.id, amount=100.0,
                              date=date(2026, 11, 5), label='CB', direction='debit',
                              bank_label='CB', bank_date=date(2026, 11, 5), bank_reference='N1',
                              classification='category', category_id=courses.id))
    _db.session.commit()
    _account(db, h.common, 1000.0)
    assert cs.measure_position(h.common, 2026, 12, date(2026, 11, 15)) == pytest.approx(1000.0 - 500.0)


def test_position_follows_carry_in_frozen_in_the_month(db, household_clean):
    """Carry switched off after November's envelope took its carry-in: still reserved (no double count)."""
    from app.services.budget_service import get_or_create_budget_month, recalculate_budget_month
    h = household_clean
    _used_october(db, h, carry_over=True)
    recalculate_budget_month(get_or_create_budget_month(h.common.id, 2026, 11))   # carry_in 800
    Category.query.filter_by(pot_id=h.common.id, name='Loyer').one().carry_over = False
    _db.session.commit()
    plan = _ready(db, h, balance=500.0)
    assert plan.position == pytest.approx(500.0 - 800.0)


def test_position_keeps_pending_direct_debit_of_a_used_previous_month(db, household_clean):
    """A direct debit (fixed_auto) of October not fully passed will still come: kept reserved."""
    h = household_clean
    _used_october(db, h, carry_over=False)
    Category.query.filter_by(pot_id=h.common.id, name='Loyer').one().type = 'fixed_auto'
    _db.session.commit()
    plan = _ready(db, h, balance=500.0)
    assert plan.position == pytest.approx(500.0 - 800.0)


def test_position_keeps_carried_remaining_of_a_used_previous_month(db, household_clean):
    """With carry-over the 800 € roll into November's envelope (carry-ins are out of the need)."""
    h = household_clean
    _used_october(db, h, carry_over=True)
    plan = _ready(db, h, balance=500.0)
    assert plan.position == pytest.approx(500.0 - 800.0)


# --- a member's extra transfer lowers what the others still owe ----------------------------------

def _dues(plan):
    return {m.user.id: m.due for m in cs.member_status(plan)}


def test_extra_transfer_lowers_the_other_members_due(db, household_clean):
    h = household_clean
    plan, _ = _ref(db, h)                                    # transfers A 1100, B 900
    _contribution(h, h.user_b, 1500.0, date(2026, 11, 16))   # 600 € more than B's transfer
    assert _dues(plan) == {h.user_a.id: pytest.approx(500.0), h.user_b.id: 0.0}
    from app.services.balance_service import free_money
    from app.services.budget_service import get_or_create_budget_month
    fm = free_money(h.common, get_or_create_budget_month(h.common.id, *M))
    assert fm.transfer_expected == pytest.approx(500.0)


def test_extra_transfer_beyond_the_others_due_is_surplus(db, household_clean):
    h = household_clean
    plan, _ = _ref(db, h)
    _contribution(h, h.user_b, 3000.0, date(2026, 11, 16))
    assert _dues(plan) == {h.user_a.id: 0.0, h.user_b.id: 0.0}
    assert cs.common_income(h.common, *M).received == pytest.approx(3000.0)


def test_offset_dues_prorata():
    assert cs.offset_dues({1: 100.0, 2: 300.0, 3: 0.0}, 200.0) == {1: 50.0, 2: 150.0, 3: 0.0}
    assert cs.offset_dues({1: 100.0, 2: 300.0}, 0.0) == {1: 100.0, 2: 300.0}
    assert cs.offset_dues({1: 100.0, 2: 300.0}, 999.0) == {1: 0.0, 2: 0.0}


def test_dashboard_transfer_category_shows_the_real_due(db, household_clean):
    from app.models import Category
    from app.services.budget_service import (
        compute_daily_metrics, get_or_create_budget_month, get_spent_for_category)
    h = household_clean
    _ref(db, h)                                              # transfers A 1100, B 900
    _contribution(h, h.user_b, 1500.0, date(2026, 11, 16))   # B's 600 extra lower A's due to 500
    cat = Category.query.filter_by(pot_id=h.personal_a.id, role='common_transfer').first()
    if cat is None:
        cat = Category(name='Virement vers le commun', type='fixed_amount', amount=1100.0,
                       pot_id=h.personal_a.id, role='common_transfer', carry_over=False)
        _db.session.add(cat)
        _db.session.commit()
    bm = get_or_create_budget_month(h.personal_a.id, *M)
    m = compute_daily_metrics(bm, cat, 15, 30, get_spent_for_category(bm, cat))
    assert m['balance'] == pytest.approx(500.0 + 800.0)       # due + Prêt not yet debited
    assert m['remaining_daily'] == pytest.approx(1300.0 / 15)


def test_transfer_gauge_targets_paid_plus_due(db, household_clean):
    """The gauge aims at what was transferred + what is still due, not the plan's raw transfer."""
    from app.models import Category
    from app.services.budget_service import compute_daily_metrics, get_or_create_budget_month
    h = household_clean
    _ref(db, h)                                              # transfers A 1100, B 900
    _contribution(h, h.user_b, 1500.0, date(2026, 11, 16))   # B's 600 extra lower A's due to 500
    cat = Category.query.filter_by(pot_id=h.personal_a.id, role='common_transfer').first()
    if cat is None:
        cat = Category(name='Virement vers le commun', type='fixed_amount', amount=1100.0,
                       pot_id=h.personal_a.id, role='common_transfer', carry_over=False)
        _db.session.add(cat)
        _db.session.commit()
    bm = get_or_create_budget_month(h.personal_a.id, *M)
    m = compute_daily_metrics(bm, cat, 15, 30, 300.0)        # A already sent 300
    assert m['effective'] == pytest.approx(300.0 + 500.0 + 800.0)   # paid + due + Prêt to debit
    assert m['effective'] - m['spent'] == pytest.approx(m['balance'])
