"""Alert menu in the navbar: real-balance overdraft levels, budget-month day, nav alert list."""
from datetime import date, datetime

import pytest

from app.models import BankAccount, Category, Pot
from app.services import alert_service
from app.services.balance_service import pot_balance
from app.services.budget_service import (budget_day, get_or_create_budget_month,
                                         recalculate_budget_month)
from tests.test_alert_service import mails  # noqa: F401 (fixture)


def _acc(db, pot, value, limit=0.0, name='Compte'):
    acc = BankAccount(pot_id=pot.id, name=name, bank_name='B', balance_booked=value,
                      balance_value=value, overdraft_limit=limit, balance_at=datetime.now())
    db.session.add(acc)
    db.session.commit()
    return acc


# --- budget_day ---

def test_budget_day_follows_income_start_day(db, household_clean):
    pot = household_clean.common
    pot.income_start_day = 25
    # budget month Dec 2026 = 25 Nov → 24 Dec (30 days)
    assert budget_day(pot, 2026, 12, date(2026, 11, 27)) == (3, 30)
    assert budget_day(pot, 2026, 12, date(2026, 11, 25)) == (1, 30)
    assert budget_day(pot, 2026, 11, date(2026, 11, 27)) == (31, 31)   # past month: full


def test_budget_day_calendar_month(db, household_clean):
    pot = household_clean.common
    pot.income_start_day = 1
    assert budget_day(pot, 2026, 11, date(2026, 11, 20)) == (20, 30)
    assert budget_day(pot, 2026, 12, date(2026, 11, 20)) == (0, 31)    # not started yet


# --- real balance levels ---

def test_real_balance_levels(db, household_clean):
    pot = household_clean.common
    acc = _acc(db, pot, -350.0, limit=300.0)
    assert alert_service.real_balance_alerts(pot, pot_balance(pot)) == ['real_overdraft']
    acc.balance_value = -250.0          # 50 € left before the limit
    assert alert_service.real_balance_alerts(pot, pot_balance(pot)) == ['real_near']
    acc.balance_value = -200.0          # exactly 100 € left: fine
    assert alert_service.real_balance_alerts(pot, pot_balance(pot)) == []
    acc.balance_value = 50.0            # no overdraft allowed, 50 € left
    acc.overdraft_limit = 0.0
    assert alert_service.real_balance_alerts(pot, pot_balance(pot)) == ['real_near']


def test_real_balance_unknown(db, household_clean):
    pot = household_clean.common
    _acc(db, pot, None)
    assert alert_service.real_balance_alerts(pot, pot_balance(pot)) == []


def test_real_overdraft_mailed_once(db, household_clean, mails):
    pot = household_clean.common
    acc = _acc(db, pot, -500.0, limit=300.0)
    today = date.today()
    assert 'real_overdraft' in alert_service.check_free_money_alerts(pot, today)
    assert 'real_overdraft' not in alert_service.check_free_money_alerts(pot, today)
    assert ('[Tout Compte Fait] Découvert dépassé : ' + pot.name) in [s for _, s in mails]
    acc.balance_value = -250.0          # near the limit: shown in the menu, never mailed
    alert_service.check_free_money_alerts(pot, today)
    assert all('real_near' not in s for _, s in mails)


# --- nav alerts ---

@pytest.fixture
def pace_pot(db, household_clean):
    """A's personal pot (shown by A's pages), current budget month funded, one dynamic envelope
    of 300 €."""
    pot = household_clean.personal_a
    household_clean.client_a.post(f'/pots/{pot.id}/select')
    cat = Category(name='Restos', type='dynamic', percentage=0.3, pot_id=pot.id)
    db.session.add(cat)
    db.session.commit()
    today = date.today()
    bm = alert_service.current_budget_month(pot, today)
    bm.total_income = 1000.0
    recalculate_budget_month(bm)
    db.session.commit()
    return pot, cat, bm, today


def test_nav_alerts_sorted_by_severity(db, pace_pot, monkeypatch):
    pot, cat, bm, today = pace_pot
    _acc(db, pot, -500.0, limit=300.0)
    monkeypatch.setattr(alert_service, 'get_spent_for_category', lambda bm, c: 1000.0)
    items = alert_service.nav_alerts(pot, today)
    assert items[0].key == 'real_overdraft' and items[0].level == 'danger' and items[0].pulse
    assert 'pace' in [i.key for i in items]
    pace = next(i for i in items if i.key == 'pace')
    assert pace.level == 'warning' and 'Restos' in pace.title and 'au-dessus du rythme' in pace.detail
    assert alert_service.nav_badge(items) == ('danger', len(items), True)


def test_nav_alerts_free_overdraft_is_a_risk(db, pace_pot):
    pot, cat, bm, today = pace_pot
    _acc(db, pot, 0.0, limit=100.0)      # free = −300 → below −limit; real 0 → 100 € left: fine
    keys = [i.key for i in alert_service.nav_alerts(pot, today)]
    assert keys == ['free_overdraft']    # free_negative implied, not repeated
    item = alert_service.nav_alerts(pot, today)[0]
    assert item.level == 'warning' and 'risquez' in item.title


def test_nav_badge_ignores_ahead(db, pace_pot, monkeypatch):
    pot, cat, bm, today = pace_pot
    _acc(db, pot, 5000.0)
    monkeypatch.setattr(alert_service, 'get_spent_for_category', lambda bm, c: 0.01)
    monkeypatch.setattr(alert_service, 'budget_day', lambda *a: (20, 30))
    items = alert_service.nav_alerts(pot, today)
    assert [i.key for i in items] == ['ahead']
    assert alert_service.nav_badge(items) == (None, 0, False)


def test_nav_alerts_without_budget_month(db, household_clean):
    pot = household_clean.common
    _acc(db, pot, -500.0, limit=300.0)
    items = alert_service.nav_alerts(pot, date(2030, 1, 15))   # no month row: real balance only
    assert [i.key for i in items] == ['real_overdraft']


def test_page_shows_alert_menu_not_banners(db, household_clean, pace_pot):
    pot, cat, bm, today = pace_pot
    _acc(db, pot, -500.0, limit=300.0)
    html = household_clean.client_a.get('/').get_data(as_text=True)
    assert 'id="nav-alerts"' in html
    assert 'alert-pulse' in html
    assert 'Découvert dépassé' in html
    assert 'alert alert-danger mb-2' not in html          # old banner gone


def test_no_alert_menu_when_all_fine(db, household_clean, pace_pot):
    pot, cat, bm, today = pace_pot
    _acc(db, pot, 5000.0)
    html = household_clean.client_a.get('/').get_data(as_text=True)
    assert 'id="nav-alerts"' not in html


def test_alert_menu_on_other_pages(db, household_clean, pace_pot):
    pot, cat, bm, today = pace_pot
    _acc(db, pot, -500.0, limit=300.0)
    html = household_clean.client_a.get('/history').get_data(as_text=True)
    assert 'id="nav-alerts"' in html
