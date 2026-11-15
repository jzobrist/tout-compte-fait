"""Plan du mois: envelopes unused last month and with nothing planned are folded away."""
from datetime import date

from app.models import Category, Operation
from app.services import refloat_service as rs
from app.services.budget_service import get_or_create_budget_month, recalculate_budget_month

TODAY = date(2026, 11, 15)


def _cat(db, pot, name, type_='dynamic', pct=None, amount=None):
    c = Category(name=name, type=type_, percentage=pct, amount=amount, pot_id=pot.id, carry_over=False)
    db.session.add(c)
    db.session.commit()
    return c


def _spend(db, h, pot, cat, amount, day, ref):
    db.session.add(Operation(pot_id=pot.id, user_id=h.user_a.id, category_id=cat.id, amount=amount,
                             date=day, direction='debit', classification='category',
                             bank_reference=ref))
    db.session.commit()


def test_dormant_envelopes_are_flagged(db, household_clean):
    h = household_clean
    pot = h.personal_a
    used_last = _cat(db, pot, 'UsedLast')
    planned = _cat(db, pot, 'Planned', type_='fixed_amount', amount=100.0)
    dyn = _cat(db, pot, 'Dyn', pct=0.1)
    used_now = _cat(db, pot, 'UsedNow')
    dormant = _cat(db, pot, 'Dormant')
    dormant_fixed = _cat(db, pot, 'DormantFixed', type_='fixed_amount', amount=0.0)
    _spend(db, h, pot, used_last, 30.0, date(2026, 10, 10), 'R1')
    _spend(db, h, pot, used_now, 12.0, date(2026, 11, 3), 'R2')
    bm = get_or_create_budget_month(pot.id, 2026, 11)
    bm.total_income = 1000.0
    recalculate_budget_month(bm)

    view = rs.build_view(pot, TODAY, user=h.user_a)

    flags = {r['category'].id: r['dormant'] for r in view['envelopes']}
    assert flags[used_last.id] is False
    assert flags[planned.id] is False
    assert flags[dyn.id] is False
    assert flags[used_now.id] is False
    assert flags[dormant.id] is True
    assert flags[dormant_fixed.id] is True
    assert view['dormant_count'] == sum(flags.values())


def test_plan_page_folds_dormant_envelopes(db, household_clean):
    h = household_clean
    pot = h.personal_a
    _cat(db, pot, 'Dormant')
    resp = h.client_a.get('/plan')
    html = resp.get_data(as_text=True)
    assert 'envelopes-dormant-' in html
    assert 'Dormant' in html
