"""Dashboard: categories at 0 € budget / 0 € spent are folded away."""
from datetime import date

from app.models import Category, Operation
from app.services.budget_service import (
    get_or_create_budget_month, recalculate_budget_month, target_budget_month,
)


def _fund(pot):
    bm = get_or_create_budget_month(pot.id, *target_budget_month(date.today(), pot.income_start_day or 1))
    bm.total_income = 1000.0
    recalculate_budget_month(bm)


def _cat(db, pot, name, type_='fixed_amount', amount=None):
    c = Category(name=name, type=type_, amount=amount, pot_id=pot.id, carry_over=False)
    db.session.add(c)
    db.session.commit()
    return c


def _folded_part(html):
    start = html.find('id="categories-idle"')
    assert start != -1
    return html[:start], html[start:html.index('</details>', start)]


def test_idle_categories_are_folded(db, household_clean):
    h = household_clean
    pot = h.personal_a
    _cat(db, pot, 'Budgeted', amount=100.0)
    spent_on_zero = _cat(db, pot, 'SpentOnZero', amount=0.0)
    _cat(db, pot, 'IdleCat', amount=0.0)
    today = date.today()
    db.session.add(Operation(pot_id=pot.id, user_id=h.user_a.id, category_id=spent_on_zero.id,
                             amount=5.0, date=today, direction='debit', classification='category',
                             bank_reference='R1'))
    db.session.commit()
    _fund(pot)
    h.client_a.post(f'/pots/{pot.id}/select')

    html = h.client_a.get('/').get_data(as_text=True)

    visible, folded = _folded_part(html)
    assert 'IdleCat' in folded and 'IdleCat' not in visible
    assert 'Budgeted' in visible and 'SpentOnZero' in visible
    assert '1 catégorie à 0 €' in folded


def test_no_fold_when_nothing_idle(db, household_clean):
    h = household_clean
    pot = h.personal_a
    _cat(db, pot, 'Budgeted', amount=100.0)
    _fund(pot)
    h.client_a.post(f'/pots/{pot.id}/select')
    html = h.client_a.get('/').get_data(as_text=True)
    assert 'categories-idle' not in html
