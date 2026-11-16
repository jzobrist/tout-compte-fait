"""Fixed charges limited in time: first/last budget month (active_from / active_until)."""
from datetime import date
from types import SimpleNamespace

from app.models import Category
from app.models.budget import BudgetMonthCategory
from app.services.budget_service import (
    fixed_amount_for, get_or_create_budget_month, pot_disponible, recalculate_budget_month,
)
from app.services.category_service import active_in

DEC, MAR = date(2026, 12, 1), date(2027, 3, 1)


def _limited(**kw):
    return SimpleNamespace(amount=kw.pop('amount', 317.35), active_from=kw.pop('active_from', None),
                           active_until=kw.pop('active_until', None), type='fixed_auto', **kw)


def _cat(db, pot, name, amount=100.0, type_='fixed_auto', **kw):
    c = Category(name=name, type=type_, amount=amount, pot_id=pot.id, carry_over=False, **kw)
    db.session.add(c)
    db.session.commit()
    return c


# --- Task 1 ------------------------------------------------------------------------------------

def test_active_in_bounds():
    c = _limited(active_from=DEC, active_until=MAR)
    assert not active_in(c, 2026, 11) and not active_in(c, 2027, 4)
    assert active_in(c, 2026, 12) and active_in(c, 2027, 1) and active_in(c, 2027, 3)
    assert active_in(_limited(), 2020, 1)


def test_fixed_amount_for():
    c = _limited(active_from=DEC, active_until=MAR)
    assert fixed_amount_for(c, 2026, 11) == 0.0
    assert fixed_amount_for(c, 2027, 2) == 317.35


def _base(bm, cat):
    return BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=cat.id).one().base_allocated


def test_budget_month_zero_outside_period(db, household_clean):
    h = household_clean
    pot = h.personal_a
    ended = _cat(db, pot, 'Ended', active_until=date(2026, 10, 1))
    later = _cat(db, pot, 'Later', active_from=DEC)
    nov = get_or_create_budget_month(pot.id, 2026, 11)
    nov.total_income = 1000.0
    recalculate_budget_month(nov)
    assert _base(nov, ended) == 0 and _base(nov, later) == 0
    assert pot_disponible(nov) == 1000.0
    dec = get_or_create_budget_month(pot.id, 2026, 12)
    dec.total_income = 1000.0
    recalculate_budget_month(dec)
    assert _base(dec, later) == 100.0 and _base(dec, ended) == 0


# --- Task 2 ------------------------------------------------------------------------------------
from app.services import contribution_service as cs  # noqa: E402
from tests.test_contribution_service import _ready  # noqa: E402

OCT, NOV = date(2026, 10, 1), date(2026, 11, 1)


def test_member_base_ignores_ended_constrained(db, household_clean):
    h = household_clean
    loan = _cat(db, h.personal_a, 'Prêt', amount=500.0, constrained=True, active_until=OCT)
    assert cs.member_base(h.user_a, 2026, 11).constrained == 0
    loan.active_until = NOV
    db.session.commit()
    assert cs.member_base(h.user_a, 2026, 11).constrained == 500.0


def test_common_need_follows_period(db, household_clean):
    h = household_clean
    _cat(db, h.common, 'Futur', amount=300.0, active_from=DEC)
    assert cs._common_fixed(h.common, 2026, 11) == 0
    assert cs._common_fixed(h.common, 2026, 12) == 300.0
    plan = _ready(db, h)
    assert cs.need(plan).fixed == 0


def test_advance_ignores_ended_charge(db, household_clean):
    h = household_clean
    _cat(db, h.common, 'Prêt commun', amount=800.0, paid_by_user_id=h.user_a.id, active_until=OCT)
    assert cs.advance_for(h.common, h.user_a, 2026, 11) == 0
    assert cs.advance_pending(h.common, h.user_a, 2026, 11) == 0


def test_simulation_follows_period(db, household_clean):
    from app.services import simulation_service as sim
    from tests.test_simulation import TODAY as SIM_TODAY, _env
    h = household_clean
    _env(db, h)
    sim.save_forecast(h.user_a, h.user_a.id, 3000.0)
    sim.save_forecast(h.user_a, h.user_b.id, 2000.0)
    before = sim.simulate(h.common, h.user_a, SIM_TODAY)
    _cat(db, h.common, 'Fini', amount=500.0, active_until=OCT)
    _cat(db, h.personal_a, 'Fini perso', amount=300.0, constrained=True, active_until=OCT)
    after = sim.simulate(h.common, h.user_a, SIM_TODAY)
    assert before.error is None and after.shares == before.shares


# --- Task 3 ------------------------------------------------------------------------------------
import pytest  # noqa: E402

from app.services import category_service  # noqa: E402
from app.services.category_service import CategoryError  # noqa: E402


def test_create_category_with_period_via_form(db, household_ab):
    h = household_ab
    resp = h.client_a.post('/categories', data={
        'name': 'Vélo', 'type': 'fixed_auto', 'common': 'y', 'personal': '',
        'common-amount': '150', 'common-active_from': '2026-12', 'common-active_until': '2027-03'})
    assert resp.status_code == 302
    c = Category.query.filter_by(name='Vélo').one()
    assert (c.active_from, c.active_until) == (DEC, MAR)


def test_period_order_rejected(db, household_clean):
    h = household_clean
    c = _cat(db, h.personal_a, 'Vélo')
    with pytest.raises(CategoryError, match='Le premier mois doit précéder le dernier.'):
        category_service.update_settings(h.user_a, c, {'active_from': MAR, 'active_until': DEC})


def test_period_ignored_for_dynamic(db, household_clean):
    h = household_clean
    c = _cat(db, h.personal_a, 'Loisirs', amount=None, type_='dynamic')
    category_service.update_settings(h.user_a, c, {'active_from': DEC, 'active_until': MAR})
    assert (c.active_from, c.active_until) == (None, None)


def test_change_type_to_dynamic_clears_period(db, household_clean):
    h = household_clean
    c = _cat(db, h.personal_a, 'Vélo', active_from=DEC, active_until=MAR)
    category_service.change_type(h.user_a, c, 'dynamic')
    assert (c.active_from, c.active_until) == (None, None)


def test_period_locked_when_plan_validated(db, household_clean, monkeypatch):
    h = household_clean
    c = _cat(db, h.common, 'Prêt commun', amount=800.0, paid_by_user_id=h.user_a.id)
    monkeypatch.setattr(cs, 'advance_locked', lambda pot, today: True)
    with pytest.raises(CategoryError):
        category_service.update_settings(h.user_a, c, {'active_until': MAR})


def test_edit_page_shows_period(db, household_clean):
    h = household_clean
    c = _cat(db, h.common, 'Vélo', active_from=DEC, active_until=MAR)
    html = h.client_a.get(f'/categories/{c.id}/edit').get_data(as_text=True)
    assert 'value="2026-12"' in html and 'value="2027-03"' in html
