import pytest
from datetime import date
from app.models import User, Category, Pot
from app.services.household_service import create_household
from app.models.budget import BudgetMonth, BudgetMonthCategory
from app.models.alert import AlertLog
from app.services.alert_service import check_alerts_for_pot, alert_recipients, _was_alert_sent_recently

@pytest.fixture
def household_setup(db):
    u = User(email='alert@test.com', display_name='Alert')
    u.set_password('x')
    h = create_household('Alert Test', u)
    pot = Pot.query.filter_by(household_id=h.id, kind='common').one()
    cat = Category(name='Loisirs test', type='dynamic', percentage=0.10,
                   min_amount=50.0, pot_id=pot.id, carry_over=True)
    db.session.add(cat)
    db.session.flush()
    bm = BudgetMonth(pot_id=pot.id, year=2026, month=5, total_income=3000.0)
    db.session.add(bm)
    db.session.flush()
    bmc = BudgetMonthCategory(budget_month_id=bm.id, category_id=cat.id,
                               base_allocated=270.0, carry_in=0.0)
    db.session.add(bmc)
    db.session.commit()
    return pot, u, cat, bm

def test_overspending_alert_detected(db, household_setup):
    h, u, cat, bm = household_setup
    # day 10, ideal_cumulated = 270 * 10/31 = 87.1
    # spent 120 > 87.1 * 1.2 = 104.5 → overspending
    alerts = check_alerts_for_pot(h, bm, current_day=10, days_in_month=31,
                                         spent_by_cat={cat.id: 120.0})
    assert any(a['type'] == 'overspending' and a['category_id'] == cat.id for a in alerts)

def test_ahead_alert_detected(db, household_setup):
    h, u, cat, bm = household_setup
    # day 10, ideal_cumulated = 87.1
    # spent 10 < 87.1 * 0.5 = 43.5 → ahead
    alerts = check_alerts_for_pot(h, bm, current_day=10, days_in_month=31,
                                         spent_by_cat={cat.id: 10.0})
    assert any(a['type'] == 'ahead' and a['category_id'] == cat.id for a in alerts)

def test_no_duplicate_alert_within_week(db, household_setup):
    h, u, cat, bm = household_setup
    log = AlertLog(pot_id=h.id, category_id=cat.id, alert_type='overspending')
    db.session.add(log)
    db.session.commit()
    assert _was_alert_sent_recently(h.id, cat.id, 'overspending', days=7)

def test_no_alert_when_on_track(db, household_setup):
    h, u, cat, bm = household_setup
    # day 10, ideal_cumulated = 87.1
    # spent 80 — within 50%-120% range → no alert
    alerts = check_alerts_for_pot(h, bm, current_day=10, days_in_month=31,
                                         spent_by_cat={cat.id: 80.0})
    assert len(alerts) == 0


def test_alert_recipients(db, household_ab):
    h = household_ab
    assert sorted(alert_recipients(h.common)) == ['a@test.com', 'b@test.com']
    assert alert_recipients(h.personal_a) == ['a@test.com']


# --- J4: free money / overdraft alerts ---

from datetime import datetime

from app.models import BankAccount
from app.services import alert_service
from app.services.budget_service import get_or_create_budget_month, recalculate_budget_month
from tests.test_bank_sync import fake, make_conn, make_link  # noqa: F401 (fixture)

J4_TODAY = date(2026, 11, 15)


@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr(alert_service, 'send_alert_email',
                        lambda to, subject, body: sent.append((tuple(to), subject)))
    return sent


def _acc(db, pot, value, limit=0.0, name='Compte'):
    acc = BankAccount(pot_id=pot.id, name=name, bank_name='B', balance_booked=value,
                      balance_value=value, overdraft_limit=limit,
                      balance_at=datetime(2026, 11, 15, 6, 0) if value is not None else None)
    db.session.add(acc)
    db.session.commit()
    return acc


def _with_income(pot, income=1000.0):
    bm = get_or_create_budget_month(pot.id, 2026, 11)
    bm.total_income = income
    recalculate_budget_month(bm)
    return bm


def _rent(db, pot, amount):
    db.session.add(Category(name='Loyer', type='fixed_amount', amount=amount, pot_id=pot.id))
    db.session.commit()


def test_free_negative_fires_once_per_crossing(db, household_clean, mails):
    h = household_clean
    acc = _acc(db, h.common, 50.0)
    _rent(db, h.common, 100.0)
    _with_income(h.common)
    assert alert_service.check_free_money_alerts(h.common, J4_TODAY) == ['free_negative', 'free_overdraft']
    assert alert_service.check_free_money_alerts(h.common, J4_TODAY) == []
    assert len(mails) == 2
    acc.balance_value = 500.0
    db.session.commit()
    assert alert_service.check_free_money_alerts(h.common, J4_TODAY) == []
    acc.balance_value = 50.0
    db.session.commit()
    assert 'free_negative' in alert_service.check_free_money_alerts(h.common, J4_TODAY)
    assert len(mails) == 4


def test_overdraft_alert_uses_sum_of_limits(db, household_clean, mails):
    h = household_clean
    acc = _acc(db, h.common, 0.0, limit=300.0)
    _acc(db, h.common, 0.0, limit=200.0, name='Second')
    _rent(db, h.common, 450.0)
    _with_income(h.common)
    assert alert_service.free_money_alerts(h.common, J4_TODAY) == ['free_negative']
    acc.balance_value = -100.0
    db.session.commit()
    assert alert_service.free_money_alerts(h.common, J4_TODAY) == ['free_negative', 'free_overdraft']


def test_no_alert_when_unknown_or_no_income(db, household_clean, mails):
    h = household_clean
    _acc(db, h.common, -100.0)
    _with_income(h.common, 0.0)
    assert alert_service.check_free_money_alerts(h.common, J4_TODAY) == []
    _acc(db, h.common, None, name='CSV')
    _with_income(h.common, 1000.0)
    assert alert_service.check_free_money_alerts(h.common, J4_TODAY) == []
    assert mails == []


def test_personal_alert_only_to_owner(db, household_clean, mails):
    h = household_clean
    _acc(db, h.personal_a, -10.0, limit=100.0)
    _with_income(h.personal_a)
    alert_service.check_free_money_alerts(h.personal_a, J4_TODAY)
    assert mails == [(('a@test.com',), '[Tout Compte Fait] Argent libre négatif : ' + h.personal_a.name)]


def test_sync_triggers_free_money_check(db, household_clean, fake, monkeypatch):
    from app.services import bank_sync_service
    h = household_clean
    acc = _acc(db, h.common, 10.0)
    conn = make_conn(h.user_a, 'app-a')
    make_link(conn, acc, 'uid-1')
    checked = []
    monkeypatch.setattr(alert_service, 'check_free_money_alerts',
                        lambda pot, today: checked.append(pot.id) or [])
    bank_sync_service.sync_connection(conn, today=J4_TODAY)
    assert checked == [h.common.id]


def test_dashboard_shows_free_money_banner(db, household_clean):
    h = household_clean
    _acc(db, h.common, 50.0, limit=100.0)  # free -50: below 0 but within the overdraft limit
    _rent(db, h.common, 100.0)
    bm = get_or_create_budget_month(h.common.id, date.today().year, date.today().month)
    bm.total_income = 1000.0
    db.session.commit()
    for acc in BankAccount.query.all():
        acc.balance_at = datetime.now()
    db.session.commit()
    html = h.client_a.get('/').get_data(as_text=True)
    assert 'Argent libre négatif' in html
