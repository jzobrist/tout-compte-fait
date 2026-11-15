"""Common fixed charge paid from a member's personal account (« payé depuis le compte perso de »)."""
from datetime import date

import pytest

from app.extensions import db as _db
from app.models import Category, User
from app.services import category_service
from app.services import contribution_service as cs
from app.services.category_service import CategoryError
from tests.test_contribution_service import (  # noqa: F401
    M, TODAY, _account, _ccat, _ready, _salary, mail_outbox,
)


def _settings(cat, paid_by=None, amount=None):
    return {'common': {'amount': cat.amount if amount is None else amount, 'carry_over': False,
                       'paid_by': paid_by}}


def _set(user, cat, paid_by=None, amount=None, type_=None):
    category_service.update_family(user, cat, cat.name, type_ or cat.type, True, None,
                                   _settings(cat, paid_by, amount))


def _pret(db, h, amount=800.0):
    return _ccat(db, h.common, 'Prêt', 'fixed_auto', amount=amount)


@pytest.fixture
def today(monkeypatch):
    """advance_locked reads date.today(): pin it to TODAY (2026-11-15)."""
    import app.services.category_service as mod

    class _D(date):
        @classmethod
        def today(cls):
            return TODAY
    monkeypatch.setattr(mod, 'date', _D)
    return TODAY


# --- Task 1: setting rules ---------------------------------------------------------------------

def test_paid_by_stored_and_cleared(db, household_clean, today):
    h = household_clean
    pret = _pret(db, h)
    _set(h.user_a, pret, paid_by=h.user_a.id)
    assert pret.paid_by_user_id == h.user_a.id and pret.paid_by.id == h.user_a.id
    _set(h.user_a, pret, paid_by=0)
    assert pret.paid_by_user_id is None


def test_paid_by_refused_outside_rules(db, household_clean, today):
    h = household_clean
    dyn = _ccat(db, h.common, 'Courses', 'dynamic', pct=0.2)
    with pytest.raises(CategoryError):
        category_service.update_family(h.user_a, dyn, dyn.name, 'dynamic', True, None,
                                       {'common': {'percentage': 0.2, 'paid_by': h.user_a.id}})
    perso = _ccat(db, h.personal_a, 'Loyer A', 'fixed_amount', amount=500.0)
    with pytest.raises(CategoryError):
        category_service.update_family(h.user_a, perso, perso.name, 'fixed_amount', False, 'own',
                                       {'personal': {'amount': 500.0, 'paid_by': h.user_a.id}})
    pret = _pret(db, h)
    from app.services.household_service import create_household
    stranger = User(email='x@other.test', display_name='X')
    stranger.set_password('x')
    create_household('Autre', stranger)
    with pytest.raises(CategoryError):
        _set(h.user_a, pret, paid_by=stranger.id)
    assert pret.paid_by_user_id is None


def test_paid_by_refused_on_common_transfer(db, household_clean, today):
    h = household_clean
    cat = _ccat(db, h.common, 'Virement', 'fixed_amount', amount=0.0)
    cat.role = 'common_transfer'
    _db.session.commit()
    with pytest.raises(CategoryError):
        category_service._apply_settings(cat, {'amount': 0.0, 'paid_by': h.user_a.id})


def test_change_type_to_dynamic_clears_paid_by(db, household_clean, today):
    h = household_clean
    pret = _pret(db, h)
    _set(h.user_a, pret, paid_by=h.user_a.id)
    category_service.update_family(h.user_a, pret, pret.name, 'dynamic', True, None,
                                   {'common': {'percentage': 0.1}})
    assert pret.paid_by_user_id is None


def test_paid_by_locked_when_plan_validated(db, household_clean, today, mail_outbox):
    h = household_clean
    pret = _pret(db, h)
    charges = _ccat(db, h.common, 'Charges', 'fixed_amount', amount=2000.0)
    _set(h.user_a, pret, paid_by=h.user_a.id)
    plan = _ready(db, h)
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    assert cs.advance_locked(h.common, TODAY)
    with pytest.raises(CategoryError):
        _set(h.user_a, pret, paid_by=h.user_b.id)
    with pytest.raises(CategoryError):
        _set(h.user_a, pret, paid_by=h.user_a.id, amount=900.0)
    _db.session.refresh(pret)
    assert (pret.paid_by_user_id, pret.amount) == (h.user_a.id, 800.0)
    _set(h.user_a, charges, amount=2100.0)
    assert charges.amount == 2100.0


# --- Task 2: advances and transfers ------------------------------------------------------------

def _ref(db, h, a=3000.0, b=2000.0, payer='a'):
    """Reference: bases a/b, common Charges 2000 + Prêt 800 paid by `payer`, plan prepared."""
    _ccat(db, h.common, 'Charges', 'fixed_amount', amount=2000.0)
    pret = _pret(db, h)
    pret.paid_by_user_id = (h.user_a if payer == 'a' else h.user_b).id
    _db.session.commit()
    plan = _ready(db, h, a=a, b=b)
    return plan, pret


def test_shares_unchanged_transfers_net_of_advance(db, household_clean):
    h = household_clean
    plan, _ = _ref(db, h)
    A, B = h.user_a.id, h.user_b.id
    assert cs.shares(plan) == {A: 1900.0, B: 900.0}
    assert cs.transfers(plan) == {A: 1100.0, B: 900.0}
    a = next(m for m in cs.member_status(plan) if m.user.id == A)
    assert (a.amount, a.share, a.advance, a.due) == (1100.0, 1900.0, 800.0, 1100.0)


def test_advance_frozen_at_validation(db, household_clean, mail_outbox):
    from app.models import ContributionShare
    h = household_clean
    plan, pret = _ref(db, h)
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    row = ContributionShare.query.filter_by(plan_id=plan.id, user_id=h.user_a.id).one()
    assert (row.amount, row.advance) == (1900.0, 800.0)
    pret.amount = 900.0
    _db.session.commit()
    assert cs.transfers(plan)[h.user_a.id] == 1100.0


def test_advance_larger_than_share_gives_zero(db, household_clean):
    h = household_clean
    plan, _ = _ref(db, h, a=1000.0, b=4000.0)
    assert cs.shares(plan)[h.user_a.id] < 800.0
    assert cs.transfers(plan)[h.user_a.id] == 0.0
    a = next(m for m in cs.member_status(plan) if m.user.id == h.user_a.id)
    assert a.due == 0.0


def test_two_categories_same_payer_add_up(db, household_clean):
    h = household_clean
    ins = _ccat(db, h.common, 'Assurance', 'fixed_auto', amount=50.0)
    ins.paid_by_user_id = h.user_a.id
    _db.session.commit()
    plan, _ = _ref(db, h)
    assert cs.advances(plan)[h.user_a.id] == 850.0


def test_cap_applies_to_transfer(db, household_clean):
    from app.models import ContributionCap
    h = household_clean
    plan, _ = _ref(db, h)
    for c in plan.caps:
        c.cap = 500.0 if c.user_id == h.user_a.id else None
    if not plan.caps:
        _db.session.add(ContributionCap(plan_id=plan.id, user_id=h.user_a.id, cap=500.0))
    _db.session.commit()
    _db.session.refresh(plan)
    assert cs.shares(plan)[h.user_a.id] == 1300.0
    assert cs.transfers(plan)[h.user_a.id] == 500.0


def test_snapshot_uses_transfers_and_goes_stale(db, household_clean):
    h = household_clean
    plan, pret = _ref(db, h)
    seen = cs.snapshot(cs.transfers(plan))
    pret.paid_by_user_id = h.user_b.id
    _db.session.commit()
    with pytest.raises(cs.ContributionError, match='stale'):
        cs.validate(plan, h.user_a, TODAY, seen_snapshot=seen)


def test_simulation_net_of_advance(db, household_clean):
    from app.services import simulation_service as sim
    from app.services.budget_service import get_or_create_budget_month
    h = household_clean
    _ccat(db, h.common, 'Charges', 'fixed_amount', amount=2000.0)
    pret = _pret(db, h)
    pret.paid_by_user_id = h.user_a.id
    _account(db, h.common, 0.0)
    get_or_create_budget_month(h.common.id, 2026, 10)
    sim.save_forecast(h.user_a, h.user_a.id, 3000.0)
    sim.save_forecast(h.user_a, h.user_b.id, 2000.0)
    result = sim.simulate(h.common, h.user_a, TODAY)
    assert result.error is None
    assert result.shares == {h.user_a.id: 1100.0, h.user_b.id: 900.0}


# --- Task 3: free money and solidarity cap -----------------------------------------------------

from datetime import datetime  # noqa: E402

from app.models import BankAccount, BudgetMonth, Operation  # noqa: E402
from app.services.balance_service import category_remaining, free_money  # noqa: E402
from app.services.budget_service import get_or_create_budget_month  # noqa: E402
from app.services.pointage_service import classify  # noqa: E402


def _perso_account(db, h, value=2000.0, name='Perso A'):
    acc = BankAccount(pot_id=h.personal_a.id, name=name, bank_name='B', balance_booked=value,
                      balance_value=value, balance_at=datetime(2026, 11, 15, 6, 0))
    _db.session.add(acc)
    _db.session.commit()
    return acc


def _debit(db, h, acc, amount, ref, day=date(2026, 11, 5)):
    op = Operation(pot_id=acc.pot_id, bank_account_id=acc.id, user_id=h.user_a.id, amount=amount,
                   date=day, bank_date=day, direction='debit', bank_reference=ref, bank_label=ref)
    _db.session.add(op)
    _db.session.commit()
    return op


def _transfer_cat(h):
    return Category.query.filter_by(pot_id=h.personal_a.id, role='common_transfer').one()


def test_common_free_money_never_reserves_paid_charge(db, household_clean):
    h = household_clean
    plan, pret = _ref(db, h)
    assert category_remaining(get_or_create_budget_month(h.common.id, *M), pret) == 0.0
    from app.services.budget_service import recalculate_budget_month
    other = get_or_create_budget_month(h.common.id, 2027, 3)   # month without plan
    recalculate_budget_month(other)
    assert category_remaining(other, pret) == 800.0


def test_personal_free_money_reserves_charge_until_debited(db, household_clean):
    h = household_clean
    tcat = _ccat(db, h.personal_a, 'Virement vers le commun', 'fixed_amount', amount=0.0)
    tcat.role = 'common_transfer'
    _db.session.commit()
    plan, pret = _ref(db, h)
    acc = _perso_account(db, h)
    pbm = get_or_create_budget_month(h.personal_a.id, *M)
    assert category_remaining(pbm, tcat) == 1100.0 + 800.0
    classify(_debit(db, h, acc, 800.0, 'PRET'), h.user_a, 'category', category=pret)
    assert category_remaining(pbm, tcat) == 1100.0


def test_no_phantom_surplus_after_month(db, household_clean):
    h = household_clean
    plan, pret = _ref(db, h)
    charges = Category.query.filter_by(pot_id=h.common.id, name='Charges').one()
    joint = BankAccount.query.filter_by(pot_id=h.common.id).first()
    for who, amount in ((h.user_a, 1100.0), (h.user_b, 900.0)):
        op = Operation(pot_id=h.common.id, bank_account_id=joint.id, user_id=h.user_a.id,
                       amount=amount, date=date(2026, 11, 16), bank_date=date(2026, 11, 16),
                       direction='credit', bank_reference=f'VIR{who.id}', bank_label='VIR')
        _db.session.add(op)
        _db.session.commit()
        classify(op, h.user_a, 'contribution', contributor=who)
    classify(_debit(db, h, joint, 2000.0, 'CHARGES'), h.user_a, 'category', category=charges)
    classify(_debit(db, h, _perso_account(db, h), 800.0, 'PRET'), h.user_a, 'category', category=pret)
    joint.balance_value = joint.balance_booked = 0.0   # 0 + 2000 received - 2000 paid
    _db.session.commit()
    assert free_money(h.common, get_or_create_budget_month(h.common.id, *M)).value == 0.0


def test_member_cap_deducts_pending_advance(db, household_clean):
    h = household_clean
    plan, pret = _ref(db, h)
    acc = _perso_account(db, h, value=2000.0)
    with_pending = cs.member_cap(h.user_a, 2026, 11, TODAY)
    pret.paid_by_user_id = None
    _db.session.commit()
    without = cs.member_cap(h.user_a, 2026, 11, TODAY)
    assert without - with_pending == 800.0
    pret.paid_by_user_id = h.user_a.id
    _db.session.commit()
    classify(_debit(db, h, acc, 800.0, 'PRET'), h.user_a, 'category', category=pret)
    acc.balance_value = acc.balance_booked = 1200.0
    _db.session.commit()
    assert cs.member_cap(h.user_a, 2026, 11, TODAY) == with_pending
