from datetime import date, datetime

from app.models import (
    BankAccount, BudgetMonth, Category, ContributionPlan, ContributionShare, Household, Operation,
)
from app.services import contribution_service as cs
from app.services.budget_service import get_or_create_budget_month

TODAY = date(2026, 11, 15)
M = (2026, 12)


def test_caps_none_equals_equal_rav():
    s, adj, unc = cs.split_with_caps({1: 3000, 2: 2000}, 1000, {})
    assert s == cs.split_equal_rav({1: 3000, 2: 2000}, 1000) and not adj and unc == 0


def test_cap_not_binding():
    s, adj, _ = cs.split_with_caps({1: 3000, 2: 2000}, 1000, {1: 5000, 2: 500})
    assert s == {1: 1000.0, 2: 0.0} and not adj


def test_capped_member_other_compensates():
    # equal RAV: R=1500 -> {1:500, 2:500}; cap 2 at 200 -> 1 pays 800
    s, adj, unc = cs.split_with_caps({1: 2000, 2: 2000}, 1000, {2: 200})
    assert s == {1: 800.0, 2: 200.0} and adj and unc == 0


def test_cap_zero():
    s, adj, _ = cs.split_with_caps({1: 2000, 2: 2000}, 1000, {2: 0})
    assert s == {1: 1000.0, 2: 0.0} and adj


def test_caps_all_capped_leftover_is_shortfall():
    s, adj, unc = cs.split_with_caps({1: 2000, 2: 2000}, 1000, {1: 300, 2: 200})
    assert s == {1: 300.0, 2: 200.0} and adj and unc == 500.0


def test_three_members_equal_rav_among_uncapped():
    # no caps: water-filling -> {1:1250, 2:250, 3:0}
    # cap 1 at 1000 -> remaining 500 over {2,3}: R'=(2000+1000-500)/2=1250, 3 below -> 2 pays 500
    s, adj, unc = cs.split_with_caps({1: 3000, 2: 2000, 3: 1000}, 1500, {1: 1000})
    assert s == {1: 1000.0, 2: 500.0, 3: 0.0} and adj and unc == 0


def test_caps_rounding_sums_to_need():
    s, _, unc = cs.split_with_caps({1: 1000, 2: 1000, 3: 1000}, 100.01, {1: 10})
    assert round(sum(s.values()) + unc, 2) == 100.01


def _setup(db, h, balance=-300.0, overdraft=1000.0, margin=100.0, constrained=400.0,
           balance_at=datetime(2026, 11, 15, 6, 0)):
    h.user_b.household.solidarity_margin = margin
    db.session.add(BankAccount(pot_id=h.personal_b.id, name='Perso', bank_name='B',
                               balance_value=balance, balance_booked=balance, balance_at=balance_at,
                               overdraft_limit=overdraft))
    cat = Category(name='Prêt', type='fixed_amount', amount=constrained, pot_id=h.personal_b.id,
                   carry_over=False, constrained=True)
    db.session.add(cat)
    db.session.commit()
    return cat


def _spend(db, h, cat, amount, day=date(2026, 11, 3)):
    get_or_create_budget_month(h.personal_b.id, 2026, 11)  # spent only counts in an existing month
    db.session.add(Operation(pot_id=h.personal_b.id, user_id=h.user_b.id, amount=amount, date=day,
                             direction='debit', classification='category', category_id=cat.id,
                             label='Prêt'))
    db.session.commit()


def test_cap_basic(db, household_clean):
    h = household_clean
    _setup(db, h)
    # current month (400 not spent) + M (400)
    assert cs.member_cap(h.user_b, *M, TODAY, salary=2000.0) == max(
        0, -300 + 2000 - 400 - 400 + 1000 - 100) == 1800.0


def test_projection_salary_already_pointed_not_added(db, household_clean):
    h = household_clean
    _setup(db, h, overdraft=3000.0)
    get_or_create_budget_month(h.personal_b.id, *M).total_income = 2000.0
    db.session.commit()
    assert cs.member_cap(h.user_b, *M, TODAY, salary=9999.0) == cs.member_cap(h.user_b, *M, TODAY)
    assert cs.member_cap(h.user_b, *M, TODAY) == -300 - 400 - 400 + 3000 - 100


def test_projection_constrained_already_spent_not_recounted(db, household_clean):
    h = household_clean
    cat = _setup(db, h)
    _spend(db, h, cat, 400.0)
    # current month fully spent: only M's 400 counts
    assert cs.member_cap(h.user_b, *M, TODAY, salary=2000.0) == -300 + 2000 - 400 + 1000 - 100


def test_projection_current_month_is_m_counted_once(db, household_clean):
    h = household_clean
    cat = _setup(db, h)
    _spend(db, h, cat, 100.0)
    # today is inside personal month M=(2026, 11): only the 300 left to spend
    assert cs.member_cap(h.user_b, 2026, 11, TODAY, salary=2000.0) == -300 + 2000 - 300 + 1000 - 100


def test_projection_deducts_contribution_due_current_month(db, household_clean):
    h = household_clean
    _setup(db, h)
    before = cs.member_cap(h.user_b, *M, TODAY, salary=2000.0)
    plan = ContributionPlan(pot_id=h.common.id, year=2026, month=11, position=0.0, status='validated',
                            created_at=datetime(2026, 11, 10))
    plan.shares.append(ContributionShare(user_id=h.user_b.id, amount=300.0))
    db.session.add(plan)
    db.session.commit()
    assert cs.member_cap(h.user_b, *M, TODAY, salary=2000.0) == before - 300.0


def test_cap_unknown_or_stale_balance_is_none(db, household_clean):
    h = household_clean
    _setup(db, h, balance_at=datetime(2026, 11, 13, 6, 0))
    assert cs.member_cap(h.user_b, *M, TODAY, salary=2000.0) is None
    acc = BankAccount.query.filter_by(pot_id=h.personal_b.id).one()
    acc.balance_at = datetime(2026, 11, 15, 6, 0)
    acc.balance_value = None
    db.session.commit()
    assert cs.member_cap(h.user_b, *M, TODAY, salary=2000.0) is None


def test_cap_floor_zero(db, household_clean):
    h = household_clean
    _setup(db, h, balance=-5000.0)
    assert cs.member_cap(h.user_b, *M, TODAY, salary=2000.0) == 0.0


def test_member_cap_never_creates_budget_months(db, household_clean):
    h = household_clean
    _setup(db, h)
    before = BudgetMonth.query.count()
    cs.member_cap(h.user_b, *M, TODAY, salary=2000.0)
    assert BudgetMonth.query.count() == before


def test_settings_margin_saved_and_negative_rejected(household_ab, db):
    h = household_ab
    resp = h.client_a.post('/settings/household', data={'solidarity_margin': '100'})
    assert resp.status_code == 302
    db.session.expire_all()
    assert db.session.get(Household, h.user_a.household_id).solidarity_margin == 100
    resp = h.client_a.post('/settings/household', data={'solidarity_margin': '-5'}, follow_redirects=True)
    db.session.expire_all()
    assert db.session.get(Household, h.user_a.household_id).solidarity_margin == 100
    assert 'positive ou nulle' in resp.get_data(as_text=True)


# --- Task 3: caps in the real plan ---------------------------------------------------------------
import types  # noqa: E402

import pytest  # noqa: E402

from app.models import ContributionCap, Household  # noqa: E402,F811
from app.services import alert_service, pointage_service  # noqa: E402
from app.services.pointage_service import classify  # noqa: E402

MENTION = 'Montants ajustés par solidarité'
HX = {'HX-Request': 'true'}


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(alert_service, 'send_alert_email',
                        lambda to, subject, body: sent.append((tuple(to), subject, body)))
    return sent


def _salary(db, pot, amount, ym=(2026, 11)):
    get_or_create_budget_month(pot.id, *ym).total_income = amount
    db.session.commit()


def _capped_plan(db, h, a=3000.0, b=2000.0, force=False):
    """Nov plan: Loyer 2400, bases A 3000 / B 1600. B is capped at 200 (A has no known balance)."""
    _setup(db, h)
    db.session.add(BankAccount(pot_id=h.common.id, name='Commun', bank_name='B', balance_booked=0.0,
                               balance_value=0.0, balance_at=datetime(2026, 11, 15, 6, 0)))
    db.session.add(Category(name='Loyer', type='fixed_amount', amount=2400.0, pot_id=h.common.id,
                            carry_over=False))
    db.session.commit()
    _salary(db, h.personal_a, a)
    if b is not None:
        _salary(db, h.personal_b, b)
    return cs.prepare_if_ready(h.common, TODAY, force=force)


def _set_b_balance(db, h, value):
    acc = BankAccount.query.filter_by(pot_id=h.personal_b.id).one()
    acc.balance_value = acc.balance_booked = value
    db.session.commit()


def test_prepare_measures_caps(db, household_clean):
    h = household_clean
    plan = _capped_plan(db, h)
    assert {c.user_id: c.cap for c in plan.caps} == {h.user_a.id: None, h.user_b.id: 200.0}
    assert cs.shares(plan) == {h.user_a.id: 2200.0, h.user_b.id: 200.0}
    assert cs.adjusted(plan)


def test_no_cap_means_equal_rav_shares(db, household_clean):
    h = household_clean
    plan = _capped_plan(db, h)
    ContributionCap.query.filter_by(plan_id=plan.id).update({'cap': None})
    db.session.commit()
    db.session.refresh(plan)
    assert cs.shares(plan) == {h.user_a.id: 1900.0, h.user_b.id: 500.0}
    assert not cs.adjusted(plan)


def test_salary_pointed_on_draft_remeasures(db, household_clean, monkeypatch):
    h = household_clean
    plan = _capped_plan(db, h, b=None, force=True)
    assert plan.forced and {c.user_id: c.cap for c in plan.caps}[h.user_b.id] == 200.0
    _set_b_balance(db, h, -100.0)
    acc = BankAccount.query.filter_by(pot_id=h.personal_b.id).one()
    op = Operation(pot_id=h.personal_b.id, bank_account_id=acc.id, user_id=h.user_b.id, amount=2000.0,
                   date=date(2026, 11, 10), bank_date=date(2026, 11, 10), direction='credit',
                   bank_label='SALAIRE', label='SALAIRE', bank_reference='SAL-1')
    db.session.add(op)
    db.session.commit()

    class FakeDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 15)
    import datetime as dt
    fake = types.SimpleNamespace(**{k: v for k, v in vars(dt).items() if not k.startswith('__')})
    fake.date = FakeDate
    monkeypatch.setattr(pointage_service, 'datetime', fake)
    classify(op, h.user_b, 'income')
    db.session.expire_all()
    plan = cs.get_plan(h.common, 2026, 11)
    assert {c.user_id: c.cap for c in plan.caps}[h.user_b.id] == 400.0


def test_remeasure_drops_stale_validation(db, household_clean, outbox):
    h = household_clean
    plan = _capped_plan(db, h)
    cs.validate(plan, h.user_a, TODAY)
    assert cs.is_validated_by(plan, h.user_a)
    _set_b_balance(db, h, -100.0)
    cs.measure_caps(plan, TODAY)
    assert cs.shares(plan)[h.user_b.id] == 400.0
    assert not cs.is_validated_by(plan, h.user_a)


def test_reopen_remeasures(db, household_clean, outbox):
    h = household_clean
    plan = _capped_plan(db, h)
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    assert plan.status == 'validated' and plan.solidarity_adjusted
    _set_b_balance(db, h, 5000.0)
    cs.reopen(plan, h.user_a, TODAY)
    assert plan.status == 'draft' and not plan.solidarity_adjusted
    assert {c.user_id: c.cap for c in plan.caps}[h.user_b.id] > 500.0
    assert cs.shares(plan) == {h.user_a.id: 1900.0, h.user_b.id: 500.0}
    assert not cs.adjusted(plan)


def test_validated_plan_frozen_despite_new_overdraft(db, household_clean, outbox):
    h = household_clean
    plan = _capped_plan(db, h)
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    frozen = cs.shares(plan)
    assert frozen == {h.user_a.id: 2200.0, h.user_b.id: 200.0}
    _set_b_balance(db, h, -5000.0)
    cs.measure_caps(plan, TODAY)
    assert cs.shares(plan) == frozen and cs.adjusted(plan)


def test_shortfall_includes_uncovered_capped_excess(db, household_clean):
    h = household_clean
    plan = _capped_plan(db, h)
    # A: known balance 0, no overdraft, household margin 100 -> cap 0; B capped at 200
    db.session.add(BankAccount(pot_id=h.personal_a.id, name='Perso A', bank_name='B', balance_value=0.0,
                               balance_booked=0.0, balance_at=datetime(2026, 11, 15, 6, 0)))
    db.session.commit()
    cs.measure_caps(plan, TODAY)
    assert cs.split(plan)[2] == 2200.0
    assert cs.shortfall(plan) == 2200.0


def test_validated_negative_uses_shortfall_with_caps(db, household_clean, outbox):
    h = household_clean
    plan = _capped_plan(db, h)
    db.session.add(BankAccount(pot_id=h.personal_a.id, name='Perso A', bank_name='B', balance_value=0.0,
                               balance_booked=0.0, balance_at=datetime(2026, 11, 15, 6, 0)))
    db.session.commit()
    cs.measure_caps(plan, TODAY)
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    assert plan.validated_negative


def test_adjusted_mention_in_validation_mail(db, household_clean, outbox):
    h = household_clean
    plan = _capped_plan(db, h)
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    assert MENTION in outbox[0][2]


def test_no_mention_in_mail_without_adjustment(db, household_clean, outbox):
    h = household_clean
    plan = _capped_plan(db, h)
    ContributionCap.query.filter_by(plan_id=plan.id).update({'cap': None})
    db.session.commit()
    db.session.refresh(plan)
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    assert MENTION not in outbox[0][2]


def test_adjusted_mention_shown_on_plan_page(db, household_clean, frozen_today):
    from tests.test_privacy import _j6_secret_household, add_capped_b
    h = household_clean
    _j6_secret_household(h)
    add_capped_b(db, h, extra_overdraft=1000.0)
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    for client in (h.client_a, h.client_b):
        client.post(f'/pots/{h.common.id}/select')
        assert MENTION in client.get('/plan').get_data(as_text=True)


# --- Final review fixes --------------------------------------------------------------------------

def test_cap_adds_back_contribution_already_paid(db, household_clean, outbox):
    h = household_clean
    plan = _capped_plan(db, h)
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    # B paid 500 to the common pot: it already left the personal balance
    _set_b_balance(db, h, -800.0)
    db.session.add(Operation(pot_id=h.common.id, user_id=h.user_b.id, amount=500.0, date=TODAY,
                             direction='credit', classification='contribution',
                             contributor_id=h.user_b.id, label='Virement'))
    db.session.commit()
    cs.reopen(plan, h.user_a, TODAY)
    assert {c.user_id: c.cap for c in plan.caps}[h.user_b.id] == 200.0


def test_split_capped_share_is_float():
    s, _, _ = cs.split_with_caps({1: 2000, 2: 2000}, 1000, {2: 200})
    assert all(isinstance(v, float) for v in s.values())
    s, _, _ = cs.split_with_caps({1: 2000, 2: 2000}, 1000, {2: 0})
    assert all(isinstance(v, float) for v in s.values())


@pytest.mark.parametrize('bases,need,caps,expected', [
    ({1: 1000.0, 2: 1000.0}, 1500.0, {2: 200.0}, 300.0),
    ({1: 1000.0, 2: 1000.0}, 2500.0, {2: 200.0}, 1300.0),
    ({1: 1000.0, 2: 1000.0}, 2500.0, {1: 500.0, 2: 500.0}, 1500.0),
    ({1: 1000.0, 2: 1000.0}, 2500.0, {}, 500.0),
    ({1: 1000.0, 2: 1000.0}, 1000.0, {}, 0.0),
])
def test_shortfall_with_caps(bases, need, caps, expected):
    assert cs.shortfall_for(bases, need, caps) == expected


def test_negative_validation_mail_has_mention_when_adjusted(db, household_clean, outbox):
    h = household_clean
    plan = _capped_plan(db, h)
    db.session.add(BankAccount(pot_id=h.personal_a.id, name='Perso A', bank_name='B', balance_value=0.0,
                               balance_booked=0.0, balance_at=datetime(2026, 11, 15, 6, 0)))
    db.session.commit()
    cs.measure_caps(plan, TODAY)
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    assert plan.validated_negative and plan.solidarity_adjusted
    assert MENTION in outbox[0][2]


def test_prepare_rolls_back_when_cap_measure_fails(db, household_clean, monkeypatch):
    h = household_clean
    _setup(db, h)
    db.session.add(BankAccount(pot_id=h.common.id, name='Commun', bank_name='B', balance_booked=0.0,
                               balance_value=0.0, balance_at=datetime(2026, 11, 15, 6, 0)))
    db.session.commit()
    _salary(db, h.personal_a, 3000.0)
    _salary(db, h.personal_b, 2000.0)

    def boom(*a, **k):
        raise RuntimeError('boom')
    monkeypatch.setattr(cs, 'member_cap', boom)
    with pytest.raises(RuntimeError):
        cs.prepare_if_ready(h.common, TODAY)
    db.session.rollback()
    assert ContributionPlan.query.count() == 0


def test_cap_current_month_after_m_counts_only_current(db, household_clean):
    h = household_clean
    _setup(db, h)
    # personal month starts on the 20th: today (Nov 25) falls in personal month Dec, after M=Nov
    h.personal_b.income_start_day = 20
    db.session.commit()
    today = date(2026, 11, 25)
    acc = BankAccount.query.filter_by(pot_id=h.personal_b.id).one()
    acc.balance_at = datetime(2026, 11, 25, 6, 0)
    db.session.commit()
    # only the current month's 400 remains (M's are already behind)
    assert cs.member_cap(h.user_b, 2026, 11, today, salary=2000.0) == -300 + 2000 - 400 + 1000 - 100


def test_simulation_skips_member_without_personal_pot(db, household_clean, monkeypatch):
    from app.services import simulation_service as sim
    h = household_clean
    monkeypatch.setattr(cs, 'measure_position', lambda *a, **k: 0.0)
    db.session.delete(h.personal_b)
    db.session.commit()
    result = sim.simulate(h.common, h.user_a, TODAY)   # must not raise
    assert h.user_b.id not in result.shares
