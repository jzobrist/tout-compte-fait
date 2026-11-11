from app.models import Pot


def test_update_pot_income_start_day(household_ab, db):
    h = household_ab
    resp = h.client_a.post('/settings/pot', data={'income_start_day': 25, 'pot_id': h.common.id})
    assert resp.status_code == 302
    db.session.expire_all()
    assert db.session.get(Pot, h.common.id).income_start_day == 25
    assert db.session.get(Pot, h.personal_a.id).income_start_day == 1


def test_update_pot_targets_posted_pot_not_session(household_ab, db):
    h = household_ab
    with h.client_a.session_transaction() as sess:
        sess['current_pot_id'] = h.common.id
    resp = h.client_a.post('/settings/pot', data={
        'income_start_day': 12, 'pot_id': h.personal_a.id})
    assert resp.status_code == 302
    db.session.expire_all()
    assert db.session.get(Pot, h.personal_a.id).income_start_day == 12
    assert db.session.get(Pot, h.common.id).income_start_day == 1


def test_update_pot_foreign_pot_404(household_ab, db):
    h = household_ab
    for pot in (h.personal_b, h.personal_a):  # the other member's personal pot
        client = h.client_a if pot is h.personal_b else h.client_b
        resp = client.post('/settings/pot', data={'income_start_day': 12, 'pot_id': pot.id})
        assert resp.status_code == 404
    db.session.expire_all()
    assert db.session.get(Pot, h.personal_a.id).income_start_day == 1
    assert db.session.get(Pot, h.personal_b.id).income_start_day == 1


def test_settings_form_carries_current_pot_id(household_ab):
    h = household_ab
    with h.client_a.session_transaction() as sess:
        sess['current_pot_id'] = h.personal_a.id
    html = h.client_a.get('/settings').get_data(as_text=True)
    assert f'name="pot_id"' in html and f'value="{h.personal_a.id}"' in html


def test_update_pot_rejects_out_of_range(household_ab, db):
    h = household_ab
    h.client_a.post('/settings/pot', data={'income_start_day': 31, 'pot_id': h.common.id})
    db.session.expire_all()
    assert db.session.get(Pot, h.common.id).income_start_day == 1


def test_current_pot_falls_back_to_common_when_inaccessible(household_ab):
    h = household_ab
    with h.client_a.session_transaction() as sess:
        sess['current_pot_id'] = h.personal_b.id
    h.client_a.get('/settings')
    with h.client_a.session_transaction() as sess:
        assert sess['current_pot_id'] == h.common.id


def _use_pot(client, pot):
    with client.session_transaction() as sess:
        sess['current_pot_id'] = pot.id


def _bm(pot_id, year, month):
    from app.models.budget import BudgetMonth
    return BudgetMonth.query.filter_by(pot_id=pot_id, year=year, month=month).first()


def test_income_delete_uses_income_pot_not_session(household_ab, db):
    from datetime import date
    from app.models import Income
    h = household_ab
    _use_pot(h.client_a, h.personal_a)
    h.client_a.post('/incomes', data={'amount': 500, 'label': 'x', 'pot_id': h.personal_a.id})
    inc = Income.query.one()
    assert inc.pot_id == h.personal_a.id
    today = date.today()
    assert _bm(h.personal_a.id, today.year, today.month).total_income == 500
    _use_pot(h.client_a, h.common)
    h.client_a.post(f'/incomes/{inc.id}/delete')
    db.session.expire_all()
    assert _bm(h.personal_a.id, today.year, today.month).total_income == 0
    assert _bm(h.common.id, today.year, today.month) is None


def test_income_lands_next_month_when_after_start_day(household_ab, db, monkeypatch):
    from datetime import date
    import app.routes.incomes as mod
    h = household_ab
    h.common.income_start_day = 25
    db.session.commit()

    class FakeDate(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 27)
    monkeypatch.setattr(mod, 'date', FakeDate)
    h.client_a.post('/incomes', data={'amount': 700, 'label': 'salaire', 'pot_id': h.common.id})
    db.session.expire_all()
    assert _bm(h.common.id, 2026, 12).total_income == 700
    assert _bm(h.common.id, 2026, 11) is None


def _cat(pot_id, name):
    from app.models import Category
    return Category.query.filter_by(pot_id=pot_id, name=name).first()


def test_adjustment_uses_category_pot_and_rejects_cross_pot(household_ab, db):
    from app.models import BudgetAdjustment
    h = household_ab
    a1 = _cat(h.personal_a.id, 'Loisirs perso')
    a2 = _cat(h.personal_a.id, 'Épargne perso')
    c1 = _cat(h.common.id, 'Culture')
    _use_pot(h.client_a, h.common)  # session pot differs from the categories' pot
    h.client_a.post('/adjustments', data={
        'from_category_id': a1.id, 'to_category_id': a2.id, 'amount': 10})
    db.session.expire_all()
    adj = BudgetAdjustment.query.one()
    from app.models.budget import BudgetMonth
    assert db.session.get(BudgetMonth, adj.budget_month_id).pot_id == h.personal_a.id
    resp = h.client_a.post('/adjustments', data={
        'from_category_id': a1.id, 'to_category_id': c1.id, 'amount': 5})
    assert resp.status_code == 400
    assert BudgetAdjustment.query.count() == 1


def test_category_create_sets_pot(household_ab, db):
    from app.models import Category
    h = household_ab
    _use_pot(h.client_a, h.personal_a)
    h.client_a.post('/categories', data={
        'name': 'Nouvelle', 'type': 'dynamic', 'personal-percentage': '0.1',
        'personal-carry_over': 'y', 'personal': 'own'})
    cat = Category.query.filter_by(name='Nouvelle').one()
    assert cat.pot_id == h.personal_a.id


def test_budget_month_requires_pot():
    import pytest
    from app.services.budget_service import get_or_create_budget_month
    with pytest.raises(ValueError):
        get_or_create_budget_month(None, 2026, 11)


def test_changing_start_day_moves_recorded_incomes(household_ab, db):
    from datetime import date
    from app.models import BudgetMonth
    from app.services.income_service import record_income
    h = household_ab
    record_income(h.personal_a, h.user_a, 4310.33, 'Salaire', date(2026, 10, 28))
    record_income(h.personal_a, h.user_a, 50.0, 'Prime', date(2026, 10, 5))

    def total(month):
        bm = BudgetMonth.query.filter_by(pot_id=h.personal_a.id, year=2026, month=month).first()
        return bm.total_income if bm else 0.0

    assert (total(10), total(11)) == (4360.33, 0.0)
    h.client_a.post('/settings/pot', data={'income_start_day': 27, 'pot_id': h.personal_a.id})
    assert total(10) == 50.0
    assert total(11) == 4310.33
    h.client_a.post('/settings/pot', data={'income_start_day': 27, 'pot_id': h.personal_a.id})
    assert total(11) == 4310.33  # saving again changes nothing


def test_saving_start_day_recomputes_carry_and_resets_plans(household_clean, db):
    from datetime import date
    from app.models import BudgetMonthCategory, Category, RefloatPlan
    from app.services.budget_service import get_or_create_budget_month, recalculate_budget_month
    from app.services.income_service import record_income
    h = household_clean
    pot = h.personal_a
    loisirs = Category(name='Loisirs', type='dynamic', percentage=0.5, pot_id=pot.id, carry_over=True)
    db.session.add(loisirs)
    db.session.commit()
    record_income(pot, h.user_a, 1000.0, 'Salaire', date(2026, 10, 28))  # start day 1 -> October
    nov = get_or_create_budget_month(pot.id, 2026, 11)
    recalculate_budget_month(nov)
    bmc = BudgetMonthCategory.query.filter_by(budget_month_id=nov.id, category_id=loisirs.id).one()
    assert bmc.carry_in == 500.0  # carried from the (wrong) October allocation
    db.session.add(RefloatPlan(pot_id=pot.id, year=2026, month=11, deficit=999.0))
    db.session.commit()
    h.client_a.post('/settings/pot', data={'income_start_day': 27, 'pot_id': pot.id})
    db.session.refresh(bmc)
    assert bmc.carry_in == 0.0
    assert bmc.base_allocated == 500.0
    assert RefloatPlan.query.filter_by(pot_id=pot.id).count() == 0


def test_settings_page_shows_margin_field(household_ab):
    resp = household_ab.client_a.get('/settings')
    assert 'solidarity_margin' in resp.get_data(as_text=True)
