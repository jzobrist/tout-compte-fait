"""Dashboard gauges: done state, priority order, close/reopen from the dashboard."""
from datetime import date

from app.models import Category, Operation
from app.models.budget import BudgetMonthCategory
from app.services.budget_service import (
    get_or_create_budget_month, recalculate_budget_month, target_budget_month,
)

_ref = iter(range(10_000))


def _bm(pot):
    return get_or_create_budget_month(pot.id, *target_budget_month(date.today(), pot.income_start_day or 1))


def _fund(pot):
    bm = _bm(pot)
    bm.total_income = 1000.0
    recalculate_budget_month(bm)
    return bm


def _cat(db, h, pot, name, amount=100.0, debit=0.0):
    c = Category(name=name, type='fixed_amount', amount=amount, pot_id=pot.id, carry_over=False)
    db.session.add(c)
    db.session.commit()
    if debit:
        db.session.add(Operation(pot_id=pot.id, user_id=h.user_a.id if pot is h.personal_a else h.user_b.id,
                                 category_id=c.id, amount=debit, date=date.today(), direction='debit',
                                 classification='category', bank_reference=f'G{next(_ref)}'))
        db.session.commit()
    return c


def _card(html, cat):
    start = html.index(f'id="cat-{cat.id}"')
    end = html.find('class="cat-card', start)
    return html[start:end if end != -1 else len(html)]


def test_dashboard_order_and_done_state(db, household_clean):
    h = household_clean
    pot = h.personal_a
    done = _cat(db, h, pot, 'Doneish', debit=100.0)
    over = _cat(db, h, pot, 'Overish', debit=150.0)
    _cat(db, h, pot, 'Halfish', debit=50.0)
    _fund(pot)
    h.client_a.post(f'/pots/{pot.id}/select')

    html = h.client_a.get('/').get_data(as_text=True)

    assert html.index('Overish') < html.index('Halfish') < html.index('Doneish')
    assert 'cat-progress-bar done' in _card(html, done) and '✓' in _card(html, done)
    assert 'cat-progress-bar danger' in _card(html, over)


def test_htmx_add_operation_renders_sorted_list(db, household_clean):
    h = household_clean
    pot = h.personal_a
    _cat(db, h, pot, 'Halfish', debit=50.0)
    over = _cat(db, h, pot, 'Overish')
    _fund(pot)
    h.client_a.post(f'/pots/{pot.id}/select')

    resp = h.client_a.post('/operations', headers={'HX-Request': 'true'}, data={
        'amount': '150', 'date': date.today().isoformat(), 'label': 'x', 'category_id': over.id})

    html = resp.get_data(as_text=True)
    assert 'cat-progress-bar danger' in _card(html, over)
    assert html.index('Overish') < html.index('Halfish')


def _closed_at(bm, cat):
    bmc = BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=cat.id).first()
    return bmc.closed_at if bmc else None


def test_close_from_dashboard(db, household_clean):
    h = household_clean
    pot = h.personal_a
    half = _cat(db, h, pot, 'Halfish', debit=50.0)
    bm = _fund(pot)
    h.client_a.post(f'/pots/{pot.id}/select')

    resp = h.client_a.post(f'/category/{half.id}/close', headers={'HX-Request': 'true'})

    assert resp.status_code == 200
    card = _card(resp.get_data(as_text=True), half)
    assert 'cat-progress-bar done' in card and f'/category/{half.id}/reopen' in card
    assert _closed_at(bm, half) is not None


def test_reopen_from_dashboard(db, household_clean):
    from app.services.balance_service import close_envelope
    h = household_clean
    pot = h.personal_a
    half = _cat(db, h, pot, 'Halfish', debit=50.0)
    bm = _fund(pot)
    close_envelope(bm, half, date.today())
    h.client_a.post(f'/pots/{pot.id}/select')

    resp = h.client_a.post(f'/category/{half.id}/reopen', headers={'HX-Request': 'true'})

    assert resp.status_code == 200
    assert 'cat-progress-bar ok' in _card(resp.get_data(as_text=True), half)
    assert _closed_at(bm, half) is None


def test_close_other_personal_category_404(db, household_clean):
    h = household_clean
    theirs = _cat(db, h, h.personal_b, 'Theirs', debit=10.0)
    bm = _fund(h.personal_b)

    resp = h.client_a.post(f'/category/{theirs.id}/close', headers={'HX-Request': 'true'})

    assert resp.status_code == 404
    assert _closed_at(bm, theirs) is None


def test_no_close_button_on_past_month(db, household_clean):
    h = household_clean
    pot = h.personal_a
    half = _cat(db, h, pot, 'Halfish', debit=50.0)
    bm = _fund(pot)
    h.client_a.post(f'/pots/{pot.id}/select')
    py, pm = (bm.year - 1, 12) if bm.month == 1 else (bm.year, bm.month - 1)
    prev = get_or_create_budget_month(pot.id, py, pm)
    prev.total_income = 1000.0
    recalculate_budget_month(prev)

    past = h.client_a.get(f'/?year={py}&month={pm}').get_data(as_text=True)
    current = h.client_a.get('/').get_data(as_text=True)

    assert '/close"' not in past
    assert f'/category/{half.id}/close' in current
    assert 'hx-confirm="Clore libère le reste : seulement si tous les prélèvements sont passés."' in current
