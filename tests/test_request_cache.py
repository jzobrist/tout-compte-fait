from datetime import date

import pytest
from sqlalchemy import event

from app.models import BudgetAdjustment, BudgetMonthCategory, Category, Operation
from app.services.budget_service import (
    current_budget_month, get_category_effective_budget, recalculate_budget_month, spent_for_category,
)


@pytest.fixture
def sql_count(db):
    counter = {'n': 0}

    def count(*_):
        counter['n'] += 1
    event.listen(db.engine, 'before_cursor_execute', count)
    yield counter
    event.remove(db.engine, 'before_cursor_execute', count)


@pytest.fixture
def setup(db, household_clean):
    h = household_clean
    pot = h.personal_a
    food = Category(name='Courses', type='dynamic', percentage=0.5, pot_id=pot.id, carry_over=False)
    other = Category(name='Loisirs', type='dynamic', percentage=0.5, pot_id=pot.id, carry_over=False)
    db.session.add_all([food, other])
    db.session.commit()
    bm = current_budget_month(pot, date.today())
    bm.total_income = 1000.0
    recalculate_budget_month(bm)
    db.session.commit()
    return h, pot, food, other, bm


def _debit(db, pot, cat, amount, user):
    op = Operation(pot_id=pot.id, user_id=user.id, category_id=cat.id, amount=amount,
                   date=date.today(), label='x', direction='debit', classification='category')
    db.session.add(op)
    return op


def test_cached_within_request(app, db, setup, sql_count):
    h, pot, food, other, bm = setup
    with app.test_request_context():
        s1, e1 = spent_for_category(food, bm.year, bm.month), get_category_effective_budget(bm, food)
        before = sql_count['n']
        s2, e2 = spent_for_category(food, bm.year, bm.month), get_category_effective_budget(bm, food)
        assert sql_count['n'] == before
        assert (s1, e1) == (s2, e2)


def test_invalidated_after_flush(app, db, setup):
    h, pot, food, other, bm = setup
    with app.test_request_context():
        assert spent_for_category(food, bm.year, bm.month) == 0.0
        _debit(db, pot, food, 12.5, h.user_a)
        db.session.flush()
        assert spent_for_category(food, bm.year, bm.month) == 12.5


def test_bypassed_with_unflushed_changes(app, db, setup):
    h, pot, food, other, bm = setup
    with app.test_request_context():
        base = get_category_effective_budget(bm, food)
        bmc = BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=food.id).one()
        bmc.base_allocated += 100      # in memory only, not flushed
        assert get_category_effective_budget(bm, food) == base + 100


def test_invalidated_after_commit(app, db, setup):
    h, pot, food, other, bm = setup
    with app.test_request_context():
        base = get_category_effective_budget(bm, food)
        db.session.add(BudgetAdjustment(budget_month_id=bm.id, from_category_id=other.id,
                                        to_category_id=food.id, amount=30.0, user_id=h.user_a.id))
        db.session.commit()
        assert get_category_effective_budget(bm, food) == base + 30


def test_invalidated_after_bulk_update(app, db, setup):
    h, pot, food, other, bm = setup
    op = _debit(db, pot, food, 10.0, h.user_a)
    db.session.commit()
    with app.test_request_context():
        assert spent_for_category(food, bm.year, bm.month) == 10.0
        Operation.query.filter_by(id=op.id).update({'amount': 99.0})
        assert spent_for_category(food, bm.year, bm.month) == 99.0


def test_invalidated_after_rollback(app, db, setup):
    h, pot, food, other, bm = setup
    with app.test_request_context():
        _debit(db, pot, food, 40.0, h.user_a)
        db.session.flush()
        assert spent_for_category(food, bm.year, bm.month) == 40.0
        db.session.rollback()
        assert spent_for_category(food, bm.year, bm.month) == 0.0


def test_no_cache_outside_request(app, db, setup, sql_count, monkeypatch):
    from app.services import request_cache
    h, pot, food, other, bm = setup
    monkeypatch.setattr(request_cache, 'has_request_context', lambda: False)   # pytest-flask pushes one
    spent_for_category(food, bm.year, bm.month)
    before = sql_count['n']
    spent_for_category(food, bm.year, bm.month)
    assert sql_count['n'] > before


def test_route_post_then_gauge_fresh(db, setup):
    h, pot, food, other, bm = setup
    h.client_a.post(f'/pots/{pot.id}/select')
    h.client_a.get('/')                                   # warm page
    r = h.client_a.post('/operations', data={'amount': '45.50', 'category_id': str(food.id),
                                             'label': 'Marché', 'date': date.today().isoformat()},
                        headers={'HX-Request': 'true'})
    assert r.status_code == 200
    assert '45,50' in r.get_data(as_text=True)




def test_cleared_at_request_teardown(app, db, setup, sql_count):
    h, pot, food, other, bm = setup
    with app.test_request_context():
        spent_for_category(food, bm.year, bm.month)
    with app.test_request_context():
        before = sql_count['n']
        spent_for_category(food, bm.year, bm.month)
        assert sql_count['n'] > before
