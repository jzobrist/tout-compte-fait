from datetime import date, datetime

import pytest

from app.models import (
    BankAccount, BudgetMonth, BudgetMonthCategory, Category, ContributionCap, ContributionPlan,
    RefloatPlan, SalaryForecast,
)
from app.services import contribution_service as cs
from app.services import simulation_service as sim
from app.services.budget_service import get_or_create_budget_month

TODAY = date(2026, 11, 15)
FRESH = datetime(2026, 11, 15, 6, 0)


def _env(db, h, b_balance=-300.0, common_balance=0.0):
    """Nov scenario: Loyer 2400 on the common pot, B has a 400 constrained loan and a 1000 overdraft."""
    h.user_b.household.solidarity_margin = 100.0
    db.session.add(BankAccount(pot_id=h.personal_b.id, name='Perso', bank_name='B',
                               balance_value=b_balance, balance_booked=b_balance, balance_at=FRESH,
                               overdraft_limit=1000.0))
    if common_balance is not None:
        db.session.add(BankAccount(pot_id=h.common.id, name='Commun', bank_name='B',
                                   balance_booked=common_balance, balance_value=common_balance,
                                   balance_at=FRESH))
    db.session.add(Category(name='Loyer', type='fixed_amount', amount=2400.0, pot_id=h.common.id,
                            carry_over=False))
    db.session.add(Category(name='Prêt', type='fixed_amount', amount=400.0, pot_id=h.personal_b.id,
                            carry_over=False, constrained=True))
    db.session.commit()
    get_or_create_budget_month(h.common.id, 2026, 10)   # previous month exists in real life


def _salary(db, pot, amount, ym=(2026, 11)):
    get_or_create_budget_month(pot.id, *ym).total_income = amount
    db.session.commit()


def _counts():
    return [m.query.count() for m in (ContributionPlan, RefloatPlan, BudgetMonth, ContributionCap,
                                      BudgetMonthCategory)]


def test_simulate_matches_real_plan_with_same_inputs(db, household_clean):
    h = household_clean
    _env(db, h, b_balance=-2300.0)    # salary of B (2000) not yet landed: cap = 200
    sim.save_forecast(h.user_a, h.user_a.id, 3000.0)
    sim.save_forecast(h.user_a, h.user_b.id, 2000.0)
    result = sim.simulate(h.common, h.user_a, TODAY)
    assert (result.year, result.month) == (2026, 11) and result.error is None
    assert result.adjusted is True

    # the salaries arrive (and land in B's balance), the real plan is prepared
    _salary(db, h.personal_a, 3000.0)
    _salary(db, h.personal_b, 2000.0)
    BankAccount.query.filter_by(pot_id=h.personal_b.id).one().balance_value = -300.0
    db.session.commit()
    plan = cs.prepare_if_ready(h.common, TODAY)
    assert result.shares == cs.shares(plan)
    assert result.adjusted == cs.adjusted(plan)
    assert result.shortfall == pytest.approx(cs.shortfall(plan))


def test_simulate_writes_nothing(db, household_clean):
    h = household_clean
    _env(db, h)
    sim.save_forecast(h.user_a, h.user_a.id, 3000.0)
    sim.save_forecast(h.user_a, h.user_b.id, 2000.0)
    before = _counts()
    result = sim.simulate(h.common, h.user_a, TODAY)
    assert result.error is None and result.shares
    assert _counts() == before


def test_simulate_none_once_real_plan_exists(db, household_clean):
    h = household_clean
    _env(db, h)
    for ym in ((2026, 11), (2026, 12)):
        db.session.add(ContributionPlan(pot_id=h.common.id, year=ym[0], month=ym[1], position=0.0,
                                        status='draft', created_at=datetime(2026, 11, 10)))
    db.session.commit()
    assert sim.simulate(h.common, h.user_a, TODAY) is None


def test_simulate_moves_to_next_month_once_current_plan_exists(db, household_clean):
    h = household_clean
    _env(db, h)
    db.session.add(ContributionPlan(pot_id=h.common.id, year=2026, month=11, position=0.0,
                                    status='draft', created_at=datetime(2026, 11, 10)))
    db.session.commit()
    result = sim.simulate(h.common, h.user_a, TODAY)
    assert (result.year, result.month) == (2026, 12)


def test_simulate_unknown_common_balance(db, household_clean):
    h = household_clean
    _env(db, h, common_balance=None)
    before = _counts()
    result = sim.simulate(h.common, h.user_a, TODAY)
    assert result.error == 'Solde du commun inconnu, synchronisez les comptes.'
    assert _counts() == before


def test_simulate_without_any_budget_month_creates_none(db, household_clean):
    h = household_clean
    _env(db, h)
    BudgetMonth.query.delete()
    db.session.commit()
    assert sim.simulate(h.common, h.user_a, TODAY).error is not None
    assert BudgetMonth.query.count() == 0


def test_simulate_recomputes_with_balance_changes(db, household_clean):
    h = household_clean
    _env(db, h, b_balance=-2300.0)
    sim.save_forecast(h.user_a, h.user_a.id, 3000.0)
    sim.save_forecast(h.user_a, h.user_b.id, 2000.0)
    first = sim.simulate(h.common, h.user_a, TODAY)
    BankAccount.query.filter_by(pot_id=h.personal_b.id).one().balance_value = -1000.0
    db.session.commit()
    second = sim.simulate(h.common, h.user_a, TODAY)
    assert first.shares[h.user_b.id] == 200.0 and first.adjusted
    assert second.shares[h.user_b.id] == 500.0 and not second.adjusted
    assert first.shares != second.shares


def test_default_forecasts_prefills_own_last_salary_only(db, household_clean):
    h = household_clean
    _salary(db, h.personal_a, 2800.0, (2026, 10))
    _salary(db, h.personal_a, 3100.0, (2026, 11))
    _salary(db, h.personal_a, 0.0, (2026, 12))
    _salary(db, h.personal_b, 1900.0, (2026, 11))
    assert sim.default_forecasts(h.user_a) == {h.user_a.id: 3100.0}
    sim.save_forecast(h.user_a, h.user_b.id, 2000.0)
    assert sim.default_forecasts(h.user_a) == {h.user_a.id: 3100.0, h.user_b.id: 2000.0}
    sim.save_forecast(h.user_a, h.user_a.id, 3300.0)
    assert sim.default_forecasts(h.user_a)[h.user_a.id] == 3300.0


def test_save_forecast_update_delete_and_validation(db, household_clean):
    h = household_clean
    sim.save_forecast(h.user_a, h.user_b.id, 2000.0)
    sim.save_forecast(h.user_a, h.user_b.id, 2100.0)
    assert sim.forecasts(h.user_a) == {h.user_b.id: 2100.0}
    with pytest.raises(ValueError):
        sim.save_forecast(h.user_a, h.user_b.id, -1.0)
    sim.save_forecast(h.user_a, h.user_b.id, None)
    assert sim.forecasts(h.user_a) == {} and SalaryForecast.query.count() == 0


def test_save_forecast_other_household_member_rejected(db, household_clean):
    from app.models import User
    from app.services.household_service import create_household
    h = household_clean
    stranger = User(email='x@test.com', display_name='X')
    stranger.set_password('Password1!')
    create_household('Autre', stranger)
    db.session.commit()
    with pytest.raises(LookupError):
        sim.save_forecast(h.user_a, stranger.id, 1000.0)
    assert SalaryForecast.query.count() == 0


def test_forecasts_private_per_user(db, household_clean):
    h = household_clean
    sim.save_forecast(h.user_a, h.user_b.id, 2000.0)
    assert sim.forecasts(h.user_b) == {}
    assert sim.forecasts(h.user_a) == {h.user_b.id: 2000.0}


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_save_forecast_rejects_non_finite(db, household_clean, value):
    with pytest.raises(ValueError):
        sim.save_forecast(household_clean.user_a, household_clean.user_a.id, value)
    assert sim.forecasts(household_clean.user_a) == {}
