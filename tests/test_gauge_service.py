"""Gauge state (danger/done/warn/ok) and priority order of the dashboard gauges."""
import calendar
from datetime import date

from app.models import Category, Operation
from app.services.balance_service import close_envelope
from app.services.budget_service import (
    get_or_create_budget_month, recalculate_budget_month, target_budget_month,
)
from app.services.gauge_service import build_gauges, gauge_state


def test_states_by_level():
    assert gauge_state(50, 100, fixed=False, closed=False) == ('ok', 50)
    assert gauge_state(81, 100, fixed=False, closed=False)[0] == 'warn'
    assert gauge_state(101, 100, fixed=False, closed=False)[0] == 'danger'
    assert gauge_state(5, 0, fixed=True, closed=False) == ('danger', 101)
    assert gauge_state(0, 0, fixed=True, closed=False) == ('ok', 0)


def test_fixed_exactly_reached_is_done():
    assert gauge_state(120, 120, fixed=True, closed=False)[0] == 'done'


def test_dynamic_exactly_reached_is_not_done():
    assert gauge_state(120, 120, fixed=False, closed=False)[0] == 'warn'


def test_fixed_just_under_is_not_done():
    assert gauge_state(118.40, 120, fixed=True, closed=False)[0] == 'warn'


def test_closed_is_done_any_type():
    assert gauge_state(30, 100, fixed=False, closed=True)[0] == 'done'
    assert gauge_state(0, 100, fixed=True, closed=True)[0] == 'done'


def test_closed_but_overspent_is_danger():
    assert gauge_state(130, 100, fixed=True, closed=True)[0] == 'danger'


def test_cent_rounding():
    assert gauge_state(0.1 + 0.2, 0.3, fixed=True, closed=False)[0] == 'done'


def test_build_gauges_order(db, household_clean):
    h = household_clean
    pot = h.personal_a
    today = date.today()
    bm = get_or_create_budget_month(pot.id, *target_budget_month(today, pot.income_start_day or 1))
    bm.total_income = 1000.0
    cats = {}
    for i, (name, debit) in enumerate([('Done', 100), ('Half', 50), ('Over', 150), ('Zero', 0),
                                       ('Warn', 90), ('Closed', 10)]):
        c = Category(name=name, type='fixed_amount', amount=100.0, pot_id=pot.id, carry_over=False)
        db.session.add(c)
        db.session.commit()
        cats[name] = c
        if debit:
            db.session.add(Operation(pot_id=pot.id, user_id=h.user_a.id, category_id=c.id,
                                     amount=float(debit), date=today, direction='debit',
                                     classification='category', bank_reference=f'R{i}'))
    db.session.commit()
    recalculate_budget_month(bm)
    close_envelope(bm, cats['Closed'], today)

    gauges = build_gauges(bm, today.day, calendar.monthrange(today.year, today.month)[1])

    assert [g['category'].name for g in gauges] == ['Over', 'Warn', 'Half', 'Zero', 'Done', 'Closed']
    assert [g['state'] for g in gauges] == ['danger', 'warn', 'ok', 'ok', 'done', 'done']
