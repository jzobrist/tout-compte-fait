from datetime import date

from app.models import Category, Operation
from app.routes.dashboard import _build_chart_data
from app.services.budget_service import (
    pot_spent_total, spent_for_category,
)


def _cat(db, pot, name='Vacances'):
    c = Category(name=name, type='fixed_amount', amount=100.0, pot_id=pot.id)
    db.session.add(c)
    db.session.commit()
    return c


def _op(db, hh, cat, amount, direction='debit', classification='category', day=10):
    if direction == 'credit' and classification == 'category':
        classification = 'refund'
    op = Operation(
        pot_id=cat.pot_id, user_id=hh.user_a.id, category_id=cat.id, amount=amount,
        date=date(2026, 11, day), direction=direction, classification=classification,
    )
    db.session.add(op)
    db.session.commit()
    return op


def test_spent_counts_category_debits_minus_refunds(db, household_ab):
    cat = _cat(db, household_ab.common)
    _op(db, household_ab, cat, 300)
    _op(db, household_ab, cat, 150, direction='credit')
    assert spent_for_category(cat, 2026, 11) == 150.0


def test_spent_excludes_internal_offbudget_and_unclassified(db, household_ab):
    cat = _cat(db, household_ab.common)
    _op(db, household_ab, cat, 50, classification='internal')
    _op(db, household_ab, cat, 60, classification='off_budget')
    _op(db, household_ab, cat, 70, classification=None)
    assert spent_for_category(cat, 2026, 11) == 0.0


def test_refund_can_make_spent_negative(db, household_ab):
    cat = _cat(db, household_ab.common)
    _op(db, household_ab, cat, 20, direction='credit')
    assert spent_for_category(cat, 2026, 11) == -20.0


def test_pot_spent_total_and_chart_ignore_other_pots(db, household_ab):
    c1 = _cat(db, household_ab.common, 'C1')
    c2 = _cat(db, household_ab.common, 'C2')
    other = _cat(db, household_ab.personal_a, 'P1')
    _op(db, household_ab, c1, 40)
    _op(db, household_ab, c2, 10)
    _op(db, household_ab, c2, 5, direction='credit')
    _op(db, household_ab, other, 999)
    _op(db, household_ab, c1, 77, classification='internal')
    pid = household_ab.common.id
    assert pot_spent_total(pid, 2026, 11) == 45.0
    chart = _build_chart_data(household_ab.common, 2026, 11, 1)
    assert chart['spendings'] == [45.0]


def test_manual_add_sets_classification_and_entered_amount(db, household_ab):
    cat = _cat(db, household_ab.common)
    resp = household_ab.client_a.post('/operations', data={
        'amount': '12.5', 'category_id': str(cat.id), 'label': 'x', 'date': '2026-11-05',
    })
    assert resp.status_code in (200, 302)
    op = Operation.query.one()
    assert op.direction == 'debit'
    assert op.classification == 'category'
    assert op.entered_amount == 12.5
    assert op.is_pending and not op.is_bank_movement


def test_edit_pending_syncs_entered_amount(db, household_ab):
    cat = _cat(db, household_ab.common)
    household_ab.client_a.post('/operations', data={
        'amount': '20', 'category_id': str(cat.id), 'label': 'x', 'date': '2026-11-05',
    })
    op = Operation.query.one()
    resp = household_ab.client_a.post(f'/operations/{op.id}/edit', data={
        'amount': '25', 'category_id': str(cat.id), 'label': 'x', 'date': '2026-11-05',
    })
    assert resp.status_code == 302
    op = Operation.query.one()
    assert op.amount == 25.0 and op.entered_amount == 25.0


def test_spent_counts_over_budget_month_with_start_day(household_clean, db):
    from datetime import date
    from app.models import Category, Operation
    from app.services.budget_service import spent_for_category
    h = household_clean
    pot = h.personal_a
    pot.income_start_day = 27
    cat = Category(name='Virement vers le commun', type='fixed_amount', amount=2000.0, pot_id=pot.id)
    db.session.add(cat)
    db.session.commit()
    for day, ref in ((date(2026, 10, 29), 'A'), (date(2026, 11, 26), 'B'), (date(2026, 11, 27), 'C')):
        db.session.add(Operation(pot_id=pot.id, user_id=h.user_a.id, category_id=cat.id, amount=100.0,
                                 date=day, direction='debit', classification='category', bank_reference=ref))
    db.session.commit()
    assert spent_for_category(cat, 2026, 11) == 200.0  # Oct 27 – Nov 26
    assert spent_for_category(cat, 2026, 12) == 100.0
