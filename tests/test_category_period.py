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
