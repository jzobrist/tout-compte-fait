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


# --- Task 4: freeze / unfreeze ---------------------------------------------------------------
from app.models import ContributionCap, ContributionShare, Pot, User  # noqa: E402
from app.services import contribution_service as ccs  # noqa: E402
from tests.test_contribution_service import (  # noqa: E402
    M, TODAY as CTODAY, _ceiling_setup, _common_bm, _ready, mail_outbox,
)


def _validate_common(h):
    plan = ccs.get_plan(h.common, *M)
    ccs.validate(plan, h.user_a, CTODAY)
    ccs.validate(plan, h.user_b, CTODAY)
    return plan


def _amounts(bm):
    return {cid: e.amount for cid, e in cs.report(bm).envelopes.items()}


def test_common_validation_freezes_ceilings(db, household_clean, mail_outbox):
    h = household_clean
    plan, loisirs, vacances = _ceiling_setup(db, h)
    before = _amounts(_common_bm(h))
    _validate_common(h)
    assert cs.is_frozen(h.common, *M)
    rows = {c.category_id: c.amount for c in cs.get_plan(h.common, *M).ceilings}
    assert rows == pytest.approx(before) and set(rows) == {loisirs.id, vacances.id}
    assert _amounts(_common_bm(h)) == pytest.approx(before)


def test_freeze_includes_extra_cut_of_binding_caps(db, household_clean, mail_outbox):
    h = household_clean
    plan, loisirs, vacances = _ceiling_setup(db, h)
    ContributionCap.query.filter_by(plan_id=plan.id).delete()
    db.session.add_all([ContributionCap(plan_id=plan.id, user_id=h.user_a.id, cap=None),
                        ContributionCap(plan_id=plan.id, user_id=h.user_b.id, cap=0.0)])
    db.session.commit()
    db.session.refresh(plan)
    before = _amounts(_common_bm(h))
    assert sum(before.values()) < 2 * 260.0 - 100.0 - 1   # extra pass really cuts
    _validate_common(h)
    assert _amounts(_common_bm(h)) == pytest.approx(before)
    rows = {c.category_id: c.amount for c in cs.get_plan(h.common, *M).ceilings}
    assert rows == pytest.approx(before)


def test_income_after_validation_keeps_ceilings(db, household_clean, mail_outbox):
    h = household_clean
    plan, loisirs, vacances = _ceiling_setup(db, h)
    _validate_common(h)
    bm = _common_bm(h)
    before = {c.id: _base(bm, c).base_allocated for c in (loisirs, vacances)}
    bm.total_income = 500.0
    ccs.recalculate_household(h.common, *M)
    pbm = get_or_create_budget_month(h.personal_a.id, *M)
    pbm.total_income += 700.0
    db.session.commit()
    ccs.recalculate_household(h.common, *M)
    assert {c.id: _base(bm, c).base_allocated for c in (loisirs, vacances)} == pytest.approx(before)


def test_category_created_after_validation_is_auto(db, household_clean, mail_outbox):
    h = household_clean
    _ceiling_setup(db, h)
    _validate_common(h)
    rs.get_plan(h.common, *M).deficit = 0.0      # no refloat cut: preset == brut
    new = _cat(db, h.common, 'Cadeaux', pct=0.1)
    bm = _common_bm(h)
    ccs.recalculate_household(h.common, *M)
    env = cs.report(bm).envelopes[new.id]
    assert env.auto and env.locked
    from app.services.budget_service import dynamic_brut, pot_disponible
    assert env.amount == pytest.approx(dynamic_brut(new, pot_disponible(bm)))


def test_set_ceiling_refused_when_frozen(db, household_clean, mail_outbox):
    h = household_clean
    _, loisirs, _ = _ceiling_setup(db, h)
    _validate_common(h)
    with pytest.raises(cs.CeilingError, match='frozen'):
        cs.set_ceiling(h.common, *M, loisirs, 100.0)


def _personal_cat(db, h):
    a = _cat(db, h.personal_a, 'Perso A', pct=0.2)
    b = _cat(db, h.personal_b, 'Perso B', pct=0.2)
    return a, b


def test_personal_waits_for_common(db, household_clean, mail_outbox):
    h = household_clean
    _personal_cat(db, h)
    _ceiling_setup(db, h)
    assert cs.personal_waiting(h.personal_a, *M) is True
    with pytest.raises(cs.CeilingError, match='waiting'):
        cs.validate_personal(h.personal_a, *M)
    _validate_common(h)
    assert cs.personal_waiting(h.personal_a, *M) is False


def test_personal_waiting_false_without_j6(db, household_clean):
    h = household_clean
    assert cs.personal_waiting(h.personal_a, *M) is False


def test_validate_personal_rejects_common(db, household_clean):
    with pytest.raises(ValueError):
        cs.validate_personal(household_clean.common, *M)


def test_personal_validation_alone(db, household_clean, mail_outbox):
    h = household_clean
    a, b = _personal_cat(db, h)
    _ceiling_setup(db, h)
    _validate_common(h)
    cs.validate_personal(h.personal_a, *M)
    assert cs.is_frozen(h.personal_a, *M) and not cs.is_frozen(h.personal_b, *M)
    rows = {c.category_id for c in cs.get_plan(h.personal_a, *M).ceilings}
    assert rows == {a.id}
    with pytest.raises(cs.CeilingError, match='frozen'):
        cs.validate_personal(h.personal_a, *M)


def test_reopen_personal(db, household_clean, mail_outbox):
    h = household_clean
    a, _ = _personal_cat(db, h)
    _ceiling_setup(db, h)
    _validate_common(h)
    with pytest.raises(cs.CeilingError, match='not_frozen'):
        cs.reopen_personal(h.personal_a, *M)
    cs.validate_personal(h.personal_a, *M)
    cs.reopen_personal(h.personal_a, *M)
    assert not cs.is_frozen(h.personal_a, *M)


def test_common_reopen_unfreezes_personal_keeps_values(db, household_clean, mail_outbox):
    h = household_clean
    a, _ = _personal_cat(db, h)
    _, loisirs, _ = _ceiling_setup(db, h)
    plan = _validate_common(h)
    cs.validate_personal(h.personal_a, *M)
    kept = {c.category_id: c.amount for c in cs.get_plan(h.personal_a, *M).ceilings}
    kept_common = {c.category_id: c.amount for c in cs.get_plan(h.common, *M).ceilings}
    ccs.reopen(plan, h.user_a, CTODAY)
    assert not cs.is_frozen(h.common, *M) and not cs.is_frozen(h.personal_a, *M)
    assert {c.category_id: c.amount for c in cs.get_plan(h.personal_a, *M).ceilings} == kept
    assert {c.category_id: c.amount for c in cs.get_plan(h.common, *M).ceilings} == kept_common
    assert not cs.report(_common_bm(h)).envelopes[loisirs.id].auto


def test_single_member_household_does_not_wait(db):
    from app.services.household_service import create_household
    u = User(email='solo2@test.com', display_name='Solo')
    u.set_password('x')
    hh = create_household('Solo', u)
    personal = Pot.query.filter_by(owner_id=u.id).one()
    assert cs.personal_waiting(personal, 2026, 11) is False
    _cat(db, personal, 'X', pct=0.2)
    _bm(personal, 1000.0)
    cs.validate_personal(personal, 2026, 11)
    assert cs.is_frozen(personal, 2026, 11)


def test_validate_is_atomic_with_freeze(db, household_clean, mail_outbox, monkeypatch):
    h = household_clean
    plan, _, _ = _ceiling_setup(db, h)
    ccs.validate(plan, h.user_a, CTODAY)
    real = cs.freeze

    def freeze_then_boom(*a, **k):
        real(*a, **k)
        raise RuntimeError('boom')
    monkeypatch.setattr(cs, 'freeze', freeze_then_boom)
    with pytest.raises(RuntimeError):
        ccs.validate(plan, h.user_b, CTODAY)
    db.session.rollback()
    assert not cs.is_frozen(h.common, *M)
    assert ccs.get_plan(h.common, *M).status == 'draft'
    assert ContributionShare.query.count() == 0


def test_reopen_is_atomic_with_unfreeze(db, household_clean, mail_outbox, monkeypatch):
    h = household_clean
    _personal_cat(db, h)
    _ceiling_setup(db, h)
    plan = _validate_common(h)
    cs.validate_personal(h.personal_a, *M)
    real = cs.unfreeze

    def unfreeze_personal_boom(pot, *a, **k):
        if pot.kind == 'personal':
            raise RuntimeError('boom')
        real(pot, *a, **k)
    monkeypatch.setattr(cs, 'unfreeze', unfreeze_personal_boom)
    with pytest.raises(RuntimeError):
        ccs.reopen(plan, h.user_a, CTODAY)
    db.session.rollback()
    assert cs.is_frozen(h.common, *M) and cs.is_frozen(h.personal_a, *M)
    assert ccs.get_plan(h.common, *M).status == 'validated'


# --- final review fixes ------------------------------------------------------------------------

def test_low_bound_accounts_for_carry_in(db, household_clean):
    h = household_clean
    pot = h.personal_a
    a = Category(name='A', type='dynamic', percentage=0.2, pot_id=pot.id, carry_over=True)
    db.session.add(a)
    db.session.commit()
    prev = get_or_create_budget_month(pot.id, 2026, 10)
    db.session.add(BudgetMonthCategory(budget_month_id=prev.id, category_id=a.id,
                                       base_allocated=300.0, carry_in=0.0))
    db.session.commit()
    bm = _bm(pot, 1000.0)
    assert _base(bm, a).carry_in == pytest.approx(300.0)
    _spend(db, h, pot, a, 350.0)
    env = cs.report(bm).envelopes[a.id]
    assert env.amount == pytest.approx(200.0)
    assert env.low == pytest.approx(50.0)          # 350 spent - 300 carried in
    cs.set_ceiling(pot, 2026, 11, a, 0.0)
    assert cs.report(bm).envelopes[a.id].amount == pytest.approx(50.0)


def test_pin_current_keeps_closed_auto_envelope_ceiling(db, setup):
    h, pot, a, b, bm = setup
    _plan(db, pot, 100.0)
    before = cs.report(bm).envelopes[a.id].amount          # 160
    close_envelope(bm, a, TODAY)
    r = cs.report(bm)
    assert r.envelopes[a.id].amount == pytest.approx(before)
    assert not r.envelopes[a.id].auto
    assert EnvelopeCeiling.query.filter_by(category_id=a.id).one().amount == pytest.approx(before)


def test_close_in_j6_draft_keeps_need_and_shares(db, household_clean, mail_outbox):
    h = household_clean
    plan, loisirs, vacances = _ceiling_setup(db, h)
    bm = _common_bm(h)
    ceil_before = cs.report(bm).envelopes[loisirs.id].amount
    need_before = ccs.need(plan).total
    shares_before = dict(ccs.shares(plan))
    close_envelope(bm, loisirs, CTODAY)
    assert cs.report(bm).envelopes[loisirs.id].amount == pytest.approx(ceil_before)
    assert ccs.need(plan).total == pytest.approx(need_before)
    assert ccs.shares(plan) == pytest.approx(shares_before)


def test_close_when_frozen_does_not_pin(db, household_clean, mail_outbox):
    h = household_clean
    _, loisirs, _ = _ceiling_setup(db, h)
    _validate_common(h)
    rows = {c.category_id: c.amount for c in cs.get_plan(h.common, *M).ceilings}
    close_envelope(_common_bm(h), loisirs, CTODAY)
    assert {c.category_id: c.amount for c in cs.get_plan(h.common, *M).ceilings} == rows


def _make_legacy_validated(db, h):
    """Validated ContributionPlan from before the feature: no EnvelopePlan."""
    plan, loisirs, vacances = _ceiling_setup(db, h)
    _validate_common(h)
    ep = cs.get_plan(h.common, *M)
    EnvelopeCeiling.query.filter_by(plan_id=ep.id).delete()
    db.session.delete(ep)
    db.session.commit()
    return plan, loisirs, vacances


def test_legacy_validated_common_month_is_frozen(db, household_clean, mail_outbox):
    h = household_clean
    _, loisirs, _ = _make_legacy_validated(db, h)
    assert cs.get_plan(h.common, *M) is None
    assert cs.is_frozen(h.common, *M)
    assert cs.report(_common_bm(h)).frozen
    with pytest.raises(cs.CeilingError, match='frozen'):
        cs.set_ceiling(h.common, *M, loisirs, 300.0)
    with pytest.raises(cs.CeilingError, match='frozen'):
        rs.set_settings(rs.get_plan(h.common, *M), 2, None)


def test_build_view_lazily_freezes_legacy_validated_month(db, household_clean, mail_outbox):
    h = household_clean
    _, loisirs, vacances = _make_legacy_validated(db, h)
    rs.build_view(h.common, CTODAY, user=h.user_a)
    ep = cs.get_plan(h.common, *M)
    assert ep is not None and ep.validated_at is not None
    assert {c.category_id for c in ep.ceilings} == {loisirs.id, vacances.id}
    bm = _common_bm(h)
    before = {c.id: _base(bm, c).base_allocated for c in (loisirs, vacances)}
    bm.total_income = 5000.0
    ccs.recalculate_household(h.common, *M)
    assert {c.id: _base(bm, c).base_allocated for c in (loisirs, vacances)} == pytest.approx(before)
