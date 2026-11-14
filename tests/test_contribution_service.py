from app.models import Category
from app.services import contribution_service as cs
from app.services.budget_service import get_or_create_budget_month


def test_split_two_members():
    assert cs.split_equal_rav({1: 3000, 2: 2000}, 2400) == {1: 1700.0, 2: 700.0}


def test_split_floor_at_zero():
    assert cs.split_equal_rav({1: 5000, 2: 1000}, 2000) == {1: 2000.0, 2: 0.0}


def test_split_single_member():
    assert cs.split_equal_rav({1: 1234.5}, 800) == {1: 800.0}


def test_split_need_not_positive():
    assert cs.split_equal_rav({1: 3000, 2: 2000}, -10) == {1: 0.0, 2: 0.0}


def test_split_rounding_sums_to_need():
    out = cs.split_equal_rav({1: 1000, 2: 1000, 3: 1000}, 100)
    assert round(sum(out.values()), 2) == 100.00
    assert all(33.33 <= v <= 33.34 for v in out.values())


def test_split_negative_base():
    assert cs.split_equal_rav({1: 3000, 2: -200}, 1000) == {1: 1000.0, 2: 0.0}


def _cat(db, pot, name, type_, amount, constrained=False):
    db.session.add(Category(name=name, type=type_, amount=amount, pot_id=pot.id,
                            carry_over=False, constrained=constrained))
    db.session.commit()


def test_member_base_uses_same_label_month_and_constrained_fixed_only(db, household_clean):
    h = household_clean
    bm = get_or_create_budget_month(h.personal_a.id, 2026, 12)
    bm.total_income = 3000.0
    db.session.commit()
    _cat(db, h.personal_a, 'Prêt', 'fixed_amount', 400.0, True)
    _cat(db, h.personal_a, 'Box', 'fixed_auto', 30.0)
    _cat(db, h.personal_a, 'Loisirs', 'dynamic', None, True)
    b = cs.member_base(h.user_a, 2026, 12)
    assert (b.salary, b.constrained, b.value, b.salary_known) == (3000.0, 400.0, 2600.0, True)


def test_member_base_no_salary(db, household_clean):
    b = cs.member_base(household_clean.user_a, 2026, 12)
    assert b.salary == 0.0 and b.salary_known is False


def test_category_form_constrained_only_personal_fixed(household_ab, db):
    h = household_ab
    h.client_a.post('/categories', data={
        'name': 'Truc', 'type': 'fixed_amount', 'common': 'y', 'common-amount': '10',
        'common-constrained': 'y', 'common-provision': 'y'})
    c = Category.query.filter_by(name='Truc').one()
    assert c.constrained is False and c.provision is True


def test_category_form_constrained_stored_for_personal_fixed(household_ab, db):
    h = household_ab
    h.client_a.post('/categories', data={
        'name': 'Prêt2', 'type': 'fixed_amount', 'personal': 'own', 'personal-amount': '10',
        'personal-constrained': 'y', 'personal-provision': 'y'})
    c = Category.query.filter_by(name='Prêt2').one()
    assert c.constrained is True and c.provision is False
    h.client_a.post('/categories', data={
        'name': 'Dyn', 'type': 'dynamic', 'personal': 'own', 'personal-percentage': '0.1',
        'personal-constrained': 'y'})
    assert Category.query.filter_by(name='Dyn').one().constrained is False


def test_seed_marks_common_transfer_role(household_ab):
    c = Category.query.filter_by(pot_id=household_ab.personal_a.id, name='Virement vers le commun').one()
    assert c.role == cs.COMMON_TRANSFER_ROLE


def test_deleting_provision_category_removes_allocations(household_clean, db):
    from app.models import ContributionPlan, ProvisionAllocation
    h = household_clean
    _cat(db, h.common, 'Prov', 'fixed_amount', 10.0)
    cat = Category.query.filter_by(name='Prov').one()
    plan = ContributionPlan(pot_id=h.common.id, year=2026, month=12)
    db.session.add(plan)
    db.session.commit()
    db.session.add(ProvisionAllocation(plan_id=plan.id, category_id=cat.id, amount=5.0))
    db.session.commit()
    h.client_a.post(f'/categories/{cat.id}/delete')
    assert ProvisionAllocation.query.count() == 0


def test_category_index_renders_flag_checkboxes(household_ab):
    h = household_ab
    html = h.client_a.get('/categories').get_data(as_text=True)
    assert 'name="common-provision"' in html and 'name="personal-constrained"' in html


# --- Task 2: plan preparation, need, shares ------------------------------------------------------
from datetime import date, datetime  # noqa: E402

import pytest  # noqa: E402

from app.models import BankAccount, BudgetMonthCategory, ContributionPlan, RefloatPlan  # noqa: E402
from app.services import refloat_service as rs  # noqa: E402
from app.services.budget_service import recalculate_budget_month  # noqa: E402

TODAY = date(2026, 11, 15)
M = (2026, 11)


def _account(db, pot, value, balance_at=datetime(2026, 11, 15, 6, 0)):
    db.session.add(BankAccount(pot_id=pot.id, name='Compte', bank_name='B', balance_booked=value,
                               balance_value=value, balance_at=balance_at))
    db.session.commit()


def _salary(db, pot, amount, ym=M):
    bm = get_or_create_budget_month(pot.id, *ym)
    bm.total_income = amount
    db.session.commit()


def _ccat(db, pot, name, type_='dynamic', pct=None, amount=None, **kw):
    c = Category(name=name, type=type_, percentage=pct, amount=amount, pot_id=pot.id,
                 carry_over=False, **kw)
    db.session.add(c)
    db.session.commit()
    return c


def _base_of(bm, cat):
    return BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=cat.id).one().base_allocated


def _ready(db, h, a=3000.0, b=2000.0, balance=0.0, force=False):
    _salary(db, h.personal_a, a)
    if b is not None:
        _salary(db, h.personal_b, b)
    _account(db, h.common, balance)
    return cs.prepare_if_ready(h.common, TODAY, force=force)


def test_target_month_skips_month_measured_by_j4(db, household_clean):
    h = household_clean
    db.session.add(RefloatPlan(pot_id=h.common.id, year=2026, month=11, deficit=10.0, months=1, split=0.5))
    db.session.commit()
    assert cs.target_month(h.common, TODAY) == (2026, 12)


def test_target_month_current_when_untouched(db, household_clean):
    h = household_clean
    assert cs.target_month(h.common, TODAY) == (2026, 11)
    h.common.income_start_day = 26
    db.session.commit()
    assert cs.target_month(h.common, date(2026, 11, 27)) == (2026, 12)


def test_prepare_waits_for_both_salaries_then_force(db, household_clean):
    h = household_clean
    assert _ready(db, h, b=None) is None
    assert cs.missing_salaries(h.common, *M) == [h.user_b]
    plan = cs.prepare_if_ready(h.common, TODAY, force=True)
    assert plan.forced
    _cat(db, h.personal_b, 'Prêt', 'fixed_amount', 100.0, True)
    assert cs.bases(plan) == {h.user_a.id: 3000.0, h.user_b.id: -100.0}
    assert cs.is_j6(h.common, *M) and not cs.is_j6(h.personal_a, *M)
    assert cs.j6_started(h.common, 2026, 12) and not cs.j6_started(h.common, 2026, 10)


def test_prepare_waits_for_fresh_balance(db, household_clean):
    h = household_clean
    _salary(db, h.personal_a, 3000.0)
    _salary(db, h.personal_b, 2000.0)
    _account(db, h.common, 0.0, balance_at=datetime(2026, 11, 12, 6, 0))
    assert cs.prepare_if_ready(h.common, TODAY) is None


def test_prepare_freezes_position_and_refloat_plan(db, household_clean):
    h = household_clean
    plan = _ready(db, h, balance=-500.0)
    assert plan.position == -500.0
    rplan = rs.get_plan(h.common, *M)
    assert rplan.deficit == 500.0 and rplan.months == 1
    BankAccount.query.one().balance_value = 900.0
    db.session.commit()
    # M now has its plan: the target month moves on, the call returns the current month's plan
    assert ContributionPlan.query.count() == 1
    again = cs.prepare_if_ready(h.common, TODAY)
    assert again.id == plan.id and again.position == -500.0


def test_common_disponible_and_dynamic_from_bases(db, household_clean):
    h = household_clean
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=1500.0)
    loisirs = _ccat(db, h.common, 'Loisirs', pct=0.1)
    _ready(db, h)
    bm = get_or_create_budget_month(h.common.id, *M)
    assert cs.common_disponible(bm) == 3500.0
    assert _base_of(bm, loisirs) == pytest.approx(350.0)


def test_need_and_shares_example(db, household_clean):
    h = household_clean
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=2400.0)
    plan = _ready(db, h)
    assert cs.need(plan).total == 2400.0
    assert cs.shares(plan) == {h.user_a.id: 1700.0, h.user_b.id: 700.0}
    assert cs.shortfall(plan) == 0.0
    assert cs.contribution_for(h.personal_a, *M) == 1700.0
    assert cs.contribution_for(h.personal_a, 2026, 10) is None


def test_need_refloat_uses_uncut_budgets(db, household_clean):
    h = household_clean
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=1000.0)
    loisirs = _ccat(db, h.common, 'Loisirs', pct=0.5)
    plan = _ready(db, h, balance=-600.0)
    bm = get_or_create_budget_month(h.common.id, *M)
    n = cs.need(plan)
    # dynamic = Σ envelope ceilings (brut − the 300 auto cut); repayment is the full 600
    assert n.dynamic == pytest.approx(1700.0) and n.refloat_v == 600.0
    assert n.total == pytest.approx(1000.0 + 2000.0 + 300.0)
    assert _base_of(bm, loisirs) == pytest.approx(2000.0 - 300.0)
    assert sum(cs.shares(plan).values()) == pytest.approx(n.total)


def test_surplus_reduces_need_and_allocation_moves_it(db, household_clean):
    h = household_clean
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=2400.0)
    vac = _ccat(db, h.common, 'Vacances', 'fixed_amount', amount=0.0, provision=True)
    plan = _ready(db, h, balance=200.0)
    assert cs.need(plan).total == pytest.approx(2200.0)
    cs.set_allocation(plan, vac, 150.0)
    assert cs.need(plan).total == pytest.approx(2350.0)
    bm = get_or_create_budget_month(h.common.id, *M)
    assert _base_of(bm, vac) == 150.0
    cs.set_allocation(plan, vac, 500.0)  # clamped to the surplus
    assert plan.allocations[0].amount == 200.0
    cs.set_allocation(plan, vac, 0.0)
    assert plan.allocations == []


def test_allocation_rejects_non_provision(db, household_clean):
    h = household_clean
    plain = _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=10.0)
    perso = _ccat(db, h.personal_a, 'Prov', 'fixed_amount', amount=10.0, provision=True)
    plan = _ready(db, h, balance=100.0)
    for cat in (plain, perso):
        with pytest.raises(ValueError):
            cs.set_allocation(plan, cat, 10.0)


def test_personal_transfer_category_gets_share(db, household_clean):
    h = household_clean
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=2400.0)
    transfer = _ccat(db, h.personal_a, 'Virement vers le commun', 'fixed_amount', amount=0.0,
                     role=cs.COMMON_TRANSFER_ROLE)
    _ccat(db, h.personal_a, 'Loisirs', pct=1.0)
    _ready(db, h)
    pbm = get_or_create_budget_month(h.personal_a.id, *M)
    assert _base_of(pbm, transfer) == 1700.0
    from app.services.budget_service import pot_disponible
    assert pot_disponible(pbm) == 3000.0 - 1700.0


def test_personal_deficit_does_not_change_shares(db, household_clean):
    h = household_clean
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=2400.0)
    plan = _ready(db, h)
    before = cs.shares(plan)
    _account(db, h.personal_a, -2000.0)
    db.session.add(RefloatPlan(pot_id=h.personal_a.id, year=2026, month=11, deficit=2000.0,
                               months=1, split=0.5))
    db.session.commit()
    assert cs.shares(plan) == before


def test_recalculate_idempotent_j6(db, household_clean):
    h = household_clean
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=1000.0)
    _ccat(db, h.common, 'Loisirs', pct=0.5)
    _ccat(db, h.personal_a, 'Virement vers le commun', 'fixed_amount', amount=0.0,
          role=cs.COMMON_TRANSFER_ROLE)
    _ready(db, h, balance=-600.0)

    def snap():
        return sorted((r.budget_month_id, r.category_id, round(r.base_allocated, 6))
                      for r in BudgetMonthCategory.query.all())
    first = snap()
    for _ in range(3):
        cs.recalculate_household(h.common, *M)
    assert snap() == first


def test_j4_month_unchanged(db, household_clean):
    h = household_clean
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=600.0)
    _salary(db, h.personal_a, 3000.0)
    _salary(db, h.personal_b, 2000.0)
    _salary(db, h.common, 1000.0)
    bm = get_or_create_budget_month(h.common.id, *M)
    recalculate_budget_month(bm)
    _account(db, h.common, 100.0)
    plan = rs.ensure_plan(h.common, bm, TODAY)
    assert plan.deficit == pytest.approx(500.0)
    assert ContributionPlan.query.count() == 0
    assert sum(t.requested for t in rs.member_transfers(plan)) == pytest.approx(250.0)


def test_force_needs_at_least_one_salary(db, household_clean):
    h = household_clean
    _account(db, h.common, 0.0)
    assert cs.prepare_if_ready(h.common, TODAY, force=True) is None


def test_need_does_not_create_budget_month(db, household_clean):
    from app.models import BudgetMonth
    h = household_clean
    plan = _ready(db, h)
    BudgetMonth.query.filter_by(pot_id=h.common.id).delete()
    db.session.commit()
    assert cs.need(plan).incomes == 0.0
    assert BudgetMonth.query.filter_by(pot_id=h.common.id).count() == 0


def test_set_settings_updates_personal_transfer_with_offset_start_days(db, household_clean):
    h = household_clean
    h.common.income_start_day = 26
    h.personal_a.income_start_day = 27
    h.personal_b.income_start_day = 27
    db.session.commit()
    transfer = _ccat(db, h.personal_a, 'Virement vers le commun', 'fixed_amount', amount=0.0,
                     role=cs.COMMON_TRANSFER_ROLE)
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=1000.0)
    _ccat(db, h.common, 'Loisirs', pct=0.5)
    plan = _ready(db, h, balance=-600.0)
    rplan = rs.get_plan(h.common, *M)
    rs.set_settings(rplan, 1, 1.0)
    pbm = get_or_create_budget_month(h.personal_a.id, *M)
    assert _base_of(pbm, transfer) == pytest.approx(cs.shares(plan)[h.user_a.id])
    assert cs.need(plan).refloat_v == 600.0


# --- Task 3: contributions received, free money, pointage ---------------------------------------
from app.models import Income, Operation  # noqa: E402
from app.services import balance_service as bs  # noqa: E402
from app.services.pointage_service import classify, contribution_received, suggest  # noqa: E402


def _credit(db, h, amount, label='VIR A', day=date(2026, 11, 16)):
    acc = BankAccount.query.filter_by(pot_id=h.common.id).first()
    op = Operation(pot_id=h.common.id, bank_account_id=acc.id, user_id=h.user_a.id, amount=amount,
                   date=day, bank_date=day, direction='credit', bank_label=label, label=label,
                   bank_reference=f'{label}-{day}-{amount}')
    db.session.add(op)
    db.session.commit()
    return op


def _j6(db, h):
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=2400.0)
    return _ready(db, h)


def test_classify_contribution_tracks_received(db, household_clean):
    h = household_clean
    plan = _j6(db, h)
    classify(_credit(db, h, 1000.0), h.user_a, 'contribution', contributor=h.user_a)
    status = {m.user.id: m for m in cs.member_status(plan)}
    a = status[h.user_a.id]
    assert (a.amount, a.received, a.due, a.validated) == (1700.0, 1000.0, 700.0, False)
    assert status[h.user_b.id].due == 700.0
    assert [m.user.id for m in cs.member_status(plan)] == sorted(status)


def test_contribution_never_income(db, household_clean):
    h = household_clean
    _j6(db, h)
    bm = get_or_create_budget_month(h.common.id, *M)
    before = bm.total_income
    classify(_credit(db, h, 1700.0), h.user_a, 'contribution', contributor=h.user_a)
    assert bm.total_income == before and Income.query.count() == 0


def test_personal_free_money_uses_due(db, household_clean):
    h = household_clean
    transfer = _ccat(db, h.personal_a, 'Virement vers le commun', 'fixed_amount', amount=0.0,
                     role=cs.COMMON_TRANSFER_ROLE)
    _j6(db, h)
    pbm = get_or_create_budget_month(h.personal_a.id, *M)
    assert bs.category_remaining(pbm, transfer) == 1700.0
    classify(_credit(db, h, 1700.0), h.user_a, 'contribution', contributor=h.user_a)
    assert bs.category_remaining(pbm, transfer) == 0.0


def test_common_free_money_counts_expected(db, household_clean):
    h = household_clean
    cbm = get_or_create_budget_month(h.common.id, *M)
    assert bs.free_money(h.common, cbm).income_received is False
    _account_ready = _j6(db, h)
    fm = bs.free_money(h.common, cbm)
    assert fm.income_received is True and fm.transfer_expected == 2400.0
    classify(_credit(db, h, 1000.0), h.user_a, 'contribution', contributor=h.user_a)
    assert bs.free_money(h.common, cbm).transfer_expected == 1400.0
    assert _account_ready is not None


def test_suggest_never_income_on_common_after_j6(db, household_clean):
    h = household_clean
    _account(db, h.common, 0.0)
    old = _credit(db, h, 50.0, day=date(2026, 9, 5))
    classify(old, h.user_a, 'income')
    assert suggest(_credit(db, h, 50.0, day=date(2026, 9, 20))).classification == 'income'
    _j6(db, h)
    new = _credit(db, h, 60.0, day=date(2026, 11, 20))
    from unittest import mock
    with mock.patch('app.services.pointage_service.datetime') as dt:
        dt.date.today.return_value = TODAY
        assert suggest(new) is None


def test_refloat_transfer_rows_migrated_label(db, household_clean):
    h = household_clean
    _j6(db, h)
    classify(_credit(db, h, 100.0), h.user_a, 'contribution', contributor=h.user_b)
    html = h.client_a.get('/history').get_data(as_text=True)
    assert 'Contribution' in html


def test_position_counts_previous_month_due(db, household_clean):
    h = household_clean
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=2400.0)
    oct_ = (2026, 10)
    _salary(db, h.personal_a, 3000.0, oct_)
    _salary(db, h.personal_b, 2000.0, oct_)
    db.session.add(ContributionPlan(pot_id=h.common.id, year=2026, month=10, position=0.0,
                                    created_at=datetime(2026, 10, 1)))
    db.session.commit()
    cs.recalculate_household(h.common, *oct_)
    _account(db, h.common, 2400.0)
    classify(_credit(db, h, 1700.0, day=date(2026, 10, 12)), h.user_a, 'contribution',
             contributor=h.user_a)
    obm = get_or_create_budget_month(h.common.id, *oct_)
    assert bs.free_money(h.common, obm).transfer_expected == 700.0
    _salary(db, h.personal_a, 3000.0)
    _salary(db, h.personal_b, 2000.0)
    plan = cs.prepare_if_ready(h.common, TODAY)
    assert plan.month == 11
    assert plan.position == pytest.approx(bs.free_money(h.common, obm).value)
    assert plan.position == pytest.approx(2400.0 - 2400.0 + 700.0)


# --- Task 4: validation workflow ----------------------------------------------------------------
from app.models import ContributionShare, ContributionValidation  # noqa: E402
from app.services import alert_service  # noqa: E402


@pytest.fixture
def mail_outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(alert_service, 'send_alert_email',
                        lambda to, subject, body: sent.append((tuple(to), subject, body)))
    return sent


def _loyer_plan(db, h, **kw):
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=2400.0)
    return _ready(db, h, **kw)


def test_validate_needs_every_member(db, household_clean, mail_outbox):
    h = household_clean
    plan = _loyer_plan(db, h)
    assert cs.validate(plan, h.user_a, TODAY) is False
    assert plan.status == 'draft' and mail_outbox == []
    assert cs.is_validated_by(plan, h.user_a) and not cs.is_validated_by(plan, h.user_b)
    assert [m.validated for m in cs.member_status(plan)] == [True, False]
    assert cs.validate(plan, h.user_b, TODAY) is True
    assert plan.status == 'validated' and plan.validated_at is not None
    assert plan.validated_negative is False
    assert {s.user_id: s.amount for s in plan.shares} == {h.user_a.id: 1700.0, h.user_b.id: 700.0}
    assert len(mail_outbox) == 1
    to, subject, body = mail_outbox[0]
    assert sorted(to) == ['a@test.com', 'b@test.com']
    assert subject == '[Tout Compte Fait] Plan de novembre 2026 validé'
    assert '1700.00' in body and '700.00' in body
    with pytest.raises(cs.ContributionError):
        cs.validate(plan, h.user_a, TODAY)


def test_change_after_one_validation_stales_it(db, household_clean, mail_outbox):
    h = household_clean
    vac = _ccat(db, h.common, 'Vacances', 'fixed_amount', amount=0.0, provision=True)
    plan = _loyer_plan(db, h, balance=200.0)
    cs.validate(plan, h.user_a, TODAY)
    assert cs.is_validated_by(plan, h.user_a)
    cs.set_allocation(plan, vac, 100.0)
    assert not cs.is_validated_by(plan, h.user_a)
    assert cs.validate(plan, h.user_b, TODAY) is False
    assert cs.validate(plan, h.user_a, TODAY) is True
    assert ContributionValidation.query.count() == 2


def test_salary_after_validation_stales(db, household_clean, mail_outbox):
    h = household_clean
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=2400.0)
    plan = _ready(db, h, b=None, force=True)
    cs.validate(plan, h.user_a, TODAY)
    assert cs.is_validated_by(plan, h.user_a)
    _salary(db, h.personal_b, 2000.0)
    assert not cs.is_validated_by(plan, h.user_a)


def test_frozen_shares_ignore_later_salary(db, household_clean, mail_outbox):
    h = household_clean
    transfer = _ccat(db, h.personal_a, 'Virement vers le commun', 'fixed_amount', amount=0.0,
                     role=cs.COMMON_TRANSFER_ROLE)
    vac = _ccat(db, h.common, 'Vacances', 'fixed_amount', amount=0.0, provision=True)
    plan = _loyer_plan(db, h, balance=200.0)
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    pbm = get_or_create_budget_month(h.personal_a.id, *M)
    before = cs.shares(plan)
    assert before[h.user_a.id] == 1600.0 and _base_of(pbm, transfer) == 1600.0
    _salary(db, h.personal_a, 5000.0)
    _cat(db, h.personal_b, 'Prêt', 'fixed_amount', 500.0, True)
    _salary(db, h.common, 900.0)
    cs.set_allocation(plan, vac, 50.0)  # spending sliders stay editable
    assert plan.allocations[0].amount == 50.0
    assert cs.shares(plan) == before
    cs.recalculate_household(h.common, *M)
    assert _base_of(get_or_create_budget_month(h.personal_a.id, *M), transfer) == 1600.0
    assert cs.is_validated_by(plan, h.user_a)


def test_validated_negative_sends_alert_without_figures(db, household_clean, mail_outbox):
    h = household_clean
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=6000.0)
    plan = _ready(db, h)
    assert cs.shortfall(plan) == 1000.0
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    assert plan.validated_negative is True
    assert len(mail_outbox) == 2
    to, subject, body = mail_outbox[1]
    assert subject == '[Tout Compte Fait] Plan validé avec un reste à vivre négatif'
    assert sorted(to) == ['a@test.com', 'b@test.com']
    assert not any(ch.isdigit() for ch in body)


def test_reopen_unfreezes_until_month_end(db, household_clean, mail_outbox):
    h = household_clean
    plan = _loyer_plan(db, h)
    cs.validate(plan, h.user_a, TODAY)
    with pytest.raises(cs.ContributionError):
        cs.reopen(plan, h.user_a, TODAY)  # not validated yet
    cs.validate(plan, h.user_b, TODAY)
    cs.reopen(plan, h.user_a, TODAY)
    assert plan.status == 'draft' and plan.shares == [] and plan.validations == []
    assert ContributionShare.query.count() == 0 and ContributionValidation.query.count() == 0
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    with pytest.raises(cs.ContributionError):
        cs.reopen(plan, h.user_a, date(2026, 12, 1))
    assert plan.status == 'validated'


def test_mail_failure_does_not_break_validation(db, household_clean, monkeypatch):
    h = household_clean
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=6000.0)
    plan = _ready(db, h)
    attempts = []

    def boom(to, subject, body):
        attempts.append(subject)
        raise RuntimeError('smtp down')
    monkeypatch.setattr(alert_service, 'send_alert_email', boom)
    cs.validate(plan, h.user_a, TODAY)
    assert cs.validate(plan, h.user_b, TODAY) is True
    assert plan.status == 'validated' and len(attempts) == 2


# --- final review fixes -----------------------------------------------------------------------

def _bank_op(db, h, amount, day, direction, label):
    acc = BankAccount.query.filter_by(pot_id=h.common.id).first()
    db.session.add(Operation(pot_id=h.common.id, bank_account_id=acc.id, user_id=h.user_a.id,
                             amount=amount, date=day, bank_date=day, direction=direction,
                             bank_label=label, label=label, bank_reference=f'{label}-{day}'))
    db.session.commit()


def test_position_excludes_flows_of_the_started_month(db, household_clean):
    h = household_clean
    _salary(db, h.personal_a, 3000.0)
    _salary(db, h.personal_b, 2000.0)
    # end of previous month: 2000; rent (debit) and income (credit) of November already booked
    _account(db, h.common, 600.0)
    _bank_op(db, h, 2400.0, date(2026, 11, 1), 'debit', 'LOYER')
    _bank_op(db, h, 1000.0, date(2026, 11, 5), 'credit', 'SALAIRE')
    _bank_op(db, h, 80.0, date(2026, 10, 30), 'debit', 'OCTOBRE')  # before the month: stays
    plan = cs.prepare_if_ready(h.common, TODAY)
    assert plan.position == pytest.approx(2000.0)
    assert cs.need(plan).incomes == 0.0  # no double count with the month's income


def test_contribution_window_follows_plan_creation(db, household_clean):
    h = household_clean
    db.session.add(ContributionPlan(pot_id=h.common.id, year=2026, month=11, position=0.0,
                                    created_at=datetime(2026, 10, 27)))
    db.session.commit()
    dec = (2026, 12)
    _salary(db, h.personal_a, 3000.0, dec)
    _salary(db, h.personal_b, 2000.0, dec)
    _account(db, h.common, 0.0, balance_at=datetime(2026, 11, 27, 6, 0))
    plan = cs.prepare_if_ready(h.common, date(2026, 11, 27))
    assert plan.month == 12 and plan.created_at.date() == date(2026, 11, 27)
    classify(_credit(db, h, 500.0, day=date(2026, 11, 29)), h.user_a, 'contribution',
             contributor=h.user_a)
    assert contribution_received(h.common, h.user_a, 2026, 12) == 500.0
    assert contribution_received(h.common, h.user_a, 2026, 11) == 0.0


def test_ensure_plan_none_for_later_month_once_j6_started(db, household_clean):
    h = household_clean
    _loyer_plan(db, h)
    nxt = get_or_create_budget_month(h.common.id, 2026, 12)
    assert rs.ensure_plan(h.common, nxt, TODAY) is None


def test_validate_rejects_stale_snapshot(db, household_clean, mail_outbox):
    h = household_clean
    plan = _loyer_plan(db, h)
    seen = cs.snapshot(cs.shares(plan))
    _salary(db, h.personal_b, 2500.0)
    with pytest.raises(cs.ContributionError, match='stale'):
        cs.validate(plan, h.user_a, TODAY, seen_snapshot=seen)
    assert ContributionValidation.query.count() == 0
    assert cs.validate(plan, h.user_a, TODAY, seen_snapshot=cs.snapshot(cs.shares(plan))) is False


def test_can_force_false_when_only_sync_awaited(db, household_clean):
    h = household_clean
    _salary(db, h.personal_a, 3000.0)
    _salary(db, h.personal_b, 2000.0)
    _account(db, h.common, 0.0, balance_at=datetime(2026, 11, 12, 6, 0))
    view = rs.build_view(h.common, TODAY, user=h.user_a)['contribution']
    assert view['waiting_sync'] is True and view['can_force'] is False


def test_build_view_survives_prepare_failure(db, household_clean, monkeypatch):
    h = household_clean

    def boom(*a, **kw):
        raise RuntimeError('race')
    monkeypatch.setattr(cs, 'prepare_if_ready', boom)
    assert rs.build_view(h.common, TODAY, user=h.user_a)['contribution'] is not None


def test_salary_classified_recalculates_existing_draft(db, household_clean):
    h = household_clean
    transfer = _ccat(db, h.personal_a, 'Virement vers le commun', 'fixed_amount', amount=0.0,
                     role=cs.COMMON_TRANSFER_ROLE)
    plan = _j6(db, h)
    pbm = get_or_create_budget_month(h.personal_a.id, *M)
    assert _base_of(pbm, transfer) == 1700.0
    acc = BankAccount(pot_id=h.personal_a.id, name='A', bank_name='B', balance_value=0.0)
    db.session.add(acc)
    db.session.commit()
    op = Operation(pot_id=h.personal_a.id, bank_account_id=acc.id, user_id=h.user_a.id, amount=1000.0,
                   date=date(2026, 11, 12), bank_date=date(2026, 11, 12), direction='credit',
                   bank_label='SAL', label='SAL', bank_reference='sal1')
    db.session.add(op)
    db.session.commit()
    from unittest import mock
    with mock.patch('app.services.pointage_service.datetime') as dt:
        dt.date.today.return_value = TODAY
        classify(op, h.user_a, 'income')
    assert plan.status == 'draft'
    assert _base_of(get_or_create_budget_month(h.personal_a.id, *M), transfer) == pytest.approx(
        cs.shares(plan)[h.user_a.id])


# --- residual fix: late previous-month contribution ----------------------------------------------

def _prev_month_setup(db, h, j6=True):
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=2400.0)
    oct_ = (2026, 10)
    _salary(db, h.personal_a, 3000.0, oct_)
    _salary(db, h.personal_b, 2000.0, oct_)
    if j6:
        db.session.add(ContributionPlan(pot_id=h.common.id, year=2026, month=10, position=0.0,
                                        created_at=datetime(2026, 10, 1)))
        db.session.commit()
        cs.recalculate_household(h.common, *oct_)
    return oct_


def _oct_shares(h):
    plan = cs.get_plan(h.common, 2026, 10)
    return {s.user_id: s.amount for s in ContributionShare.query.filter_by(plan_id=plan.id)}


def test_late_previous_month_contribution_not_double_counted(db, household_clean):
    h = household_clean
    _prev_month_setup(db, h)
    _account(db, h.common, 2400.0)
    shares_before = _oct_shares(h)
    # the 700 due is paid on Nov 2: the balance already includes it
    classify(_credit(db, h, 700.0, day=date(2026, 11, 2)), h.user_a, 'contribution',
             contributor=h.user_a)
    db.session.query(BankAccount).update({'balance_value': 3100.0, 'balance_booked': 3100.0})
    db.session.commit()
    _salary(db, h.personal_a, 3000.0)
    _salary(db, h.personal_b, 2000.0)
    plan = cs.prepare_if_ready(h.common, TODAY)
    assert plan.month == 11
    assert plan.position == pytest.approx(2400.0)
    assert _oct_shares(h) == shares_before


def test_j4_previous_month_contribution_still_subtracted(db, household_clean):
    h = household_clean
    _prev_month_setup(db, h, j6=False)
    _account(db, h.common, 3100.0)
    classify(_credit(db, h, 700.0, day=date(2026, 11, 2)), h.user_a, 'contribution',
             contributor=h.user_a)
    assert cs._month_flows_in_balance(h.common, 2026, 11) == pytest.approx(700.0)


# --- Plafonds figés, Task 3: common need from ceilings ---------------------------------------
from app.models import ContributionCap  # noqa: E402
from app.services import ceiling_service  # noqa: E402


def _ceiling_setup(db, h):
    """Rent 2400, Loisirs/Vacances 10 % each, deficit 300 over 1 month, split 1/3."""
    _ccat(db, h.common, 'Loyer', 'fixed_amount', amount=2400.0)
    loisirs = _ccat(db, h.common, 'Loisirs', pct=0.1)
    vacances = _ccat(db, h.common, 'Vacances', pct=0.1)
    plan = _ready(db, h)
    rplan = rs.get_plan(h.common, *M)
    if rplan is None:
        rplan = RefloatPlan(pot_id=h.common.id, year=2026, month=11)
        db.session.add(rplan)
    rplan.deficit, rplan.months, rplan.split = 300.0, 1, 1 / 3
    db.session.commit()
    cs.recalculate_household(h.common, *M)
    return plan, loisirs, vacances


def _common_bm(h):
    return get_or_create_budget_month(h.common.id, *M)


def _sum_shares(plan):
    return sum(cs.shares(plan).values())


def test_need_equals_old_formula_at_preset(db, household_clean):
    h = household_clean
    plan, _, _ = _ceiling_setup(db, h)
    n = cs.need(plan)
    assert n.refloat_v == pytest.approx(300.0)
    assert n.total == pytest.approx(2400.0 + 520.0 + 300.0 * (1 / 3) - n.surplus - n.incomes)


def test_lowering_ceiling_lowers_shares_not_repayment(db, household_clean):
    h = household_clean
    plan, loisirs, _ = _ceiling_setup(db, h)
    before = _sum_shares(plan)
    amount = ceiling_service.report(_common_bm(h)).envelopes[loisirs.id].amount
    ceiling_service.set_ceiling(h.common, *M, loisirs, amount - 50)
    assert _sum_shares(plan) == pytest.approx(before - 50)
    assert cs.need(plan).refloat_v == pytest.approx(300.0)


def test_raising_ceiling_raises_shares(db, household_clean):
    h = household_clean
    plan, loisirs, _ = _ceiling_setup(db, h)
    before = _sum_shares(plan)
    env = ceiling_service.report(_common_bm(h)).envelopes[loisirs.id]
    ceiling_service.set_ceiling(h.common, *M, loisirs, 260.0)
    assert _sum_shares(plan) == pytest.approx(before + (260.0 - env.amount))
    assert 260.0 - env.amount > 0


def test_shortfall_absorbed_by_auto_envelopes(db, household_clean):
    h = household_clean
    plan, loisirs, vacances = _ceiling_setup(db, h)
    amounts = {c.id: ceiling_service.report(_common_bm(h)).envelopes[c.id].amount
               for c in (loisirs, vacances)}
    for cap in plan.caps:
        db.session.delete(cap)
    db.session.flush()
    db.session.add_all([ContributionCap(plan_id=plan.id, user_id=h.user_a.id, cap=None),
                        ContributionCap(plan_id=plan.id, user_id=h.user_b.id, cap=0.0)])
    db.session.commit()
    db.session.refresh(plan)
    rep = ceiling_service.report(_common_bm(h))
    lowered = sum(amounts.values()) - sum(rep.envelopes[c].amount for c in amounts)
    assert lowered > 0
    assert cs.shortfall(plan) == pytest.approx(0.0, abs=0.01)
    assert rep.unabsorbed == pytest.approx(0.0, abs=0.01)


def test_simulation_need_unchanged(db, household_clean):
    h = household_clean
    plan, _, _ = _ceiling_setup(db, h)
    n = cs.need_for(h.common, *M, cs.bases(plan), plan.position, 0.0, 100.0)
    assert n.dynamic == pytest.approx(520.0)
    assert n.total == pytest.approx(2400.0 + 520.0 + 100.0 - n.surplus - n.incomes)


def test_common_ceiling_high_bound(db, household_clean):
    h = household_clean
    _, loisirs, vacances = _ceiling_setup(db, h)
    vacances.max_amount = 400.0
    db.session.commit()
    env = ceiling_service.report(_common_bm(h)).envelopes
    assert env[loisirs.id].high == pytest.approx(520.0)      # 2 × brut 260
    assert env[vacances.id].high == pytest.approx(800.0)     # 2 × max_amount
