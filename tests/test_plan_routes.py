import re
import sys
from datetime import date, datetime

import pytest

from app.models import BankAccount, BudgetMonthCategory, Category, RefloatPlan
from app.services import contribution_service as cs
from app.services.budget_service import (
    current_budget_month, get_or_create_budget_month, recalculate_budget_month,
)

HX = {'HX-Request': 'true'}


def _now_balance_at():
    return datetime.combine(date.today(), datetime.min.time())


def _account(db, pot, value, name='Compte'):
    acc = BankAccount(pot_id=pot.id, name=name, bank_name='Banque', balance_booked=value,
                      balance_value=value, balance_at=_now_balance_at() if value is not None else None)
    db.session.add(acc)
    db.session.commit()
    return acc


def _cat(db, pot, name, type_='dynamic', pct=None, floor=None, amount=None):
    c = Category(name=name, type=type_, percentage=pct, min_amount=floor, amount=amount,
                 pot_id=pot.id, carry_over=False)
    db.session.add(c)
    db.session.commit()
    return c


def _income(pot, amount):
    bm = current_budget_month(pot, date.today())
    bm.total_income = amount
    recalculate_budget_month(bm)
    return bm


def _deficit_setup(db, h, pot):
    """Balance 100, rent 600 + Courses/Culture dynamics on 1000 income -> deficit."""
    _account(db, pot, 100.0)
    _cat(db, pot, 'Loyer', type_='fixed_amount', amount=600.0)
    courses = _cat(db, pot, 'Courses', pct=0.5, floor=100.0)
    culture = _cat(db, pot, 'Culture', pct=0.5)
    _income(pot, 1000.0)
    return courses, culture


def _select(client, pot):
    client.post(f'/pots/{pot.id}/select')


def test_plan_page_shows_balance_and_free_money(db, household_clean):
    h = household_clean
    _account(db, h.common, 1234.5)
    _cat(db, h.common, 'Loyer', type_='fixed_amount', amount=200.0)
    _income(h.common, 1000.0)
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert 'Solde réel' in html and 'Argent vraiment libre' in html
    assert '1 234,50' in html
    assert '1 034,50' in html  # 1234.50 - 200 remaining


def test_plan_page_unknown_balance_mention(db, household_clean):
    h = household_clean
    _account(db, h.common, None, name='Livret CSV')
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert 'solde inconnu — compte Livret CSV non synchronisé' in html


def test_plan_page_no_income_mention(db, household_clean):
    h = household_clean
    _account(db, h.common, 100.0)
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert 'revenus du mois pas encore reçus' in html


def test_settings_updates_months_and_split(db, household_clean):
    h = household_clean
    _deficit_setup(db, h, h.common)
    h.client_a.get('/plan')
    plan = RefloatPlan.query.filter_by(pot_id=h.common.id).one()
    assert plan.deficit > 0
    resp = h.client_a.post(f'/plan/{h.common.id}/settings', headers=HX,
                           data={'months': '3', 'split': '70'})
    assert resp.status_code == 200
    db.session.refresh(plan)
    assert (plan.months, plan.split) == (3, 0.7)
    assert 'id="envelopes-' in resp.get_data(as_text=True)


def test_close_and_reopen_envelope_routes(db, household_clean):
    h = household_clean
    rent = _cat(db, h.common, 'Loyer', type_='fixed_amount', amount=600.0)
    bm = _income(h.common, 1000.0)
    resp = h.client_a.post(f'/plan/{h.common.id}/envelope/{rent.id}/close', headers=HX)
    assert resp.status_code == 200
    bmc = BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=rent.id).one()
    assert bmc.closed_at is not None
    assert 'Rouvrir' in resp.get_data(as_text=True)
    h.client_a.post(f'/plan/{h.common.id}/envelope/{rent.id}/reopen', headers=HX)
    db.session.refresh(bmc)
    assert bmc.closed_at is None


def test_close_past_month_refused(db, household_clean):
    h = household_clean
    rent = _cat(db, h.common, 'Loyer', type_='fixed_amount', amount=600.0)
    resp = h.client_a.post(f'/plan/{h.common.id}/envelope/{rent.id}/close', headers=HX,
                           data={'year': '2020', 'month': '1'})
    assert resp.status_code == 400


def test_close_other_household_404(db, household_clean):
    from app.models import Pot, User
    from app.services.household_service import create_household
    h = household_clean
    u = User(email='z@test.com', display_name='Z')
    u.set_password('x')
    other = create_household('Autre', u)
    other_common = Pot.query.filter_by(household_id=other.id, kind='common').one()
    cat = _cat(db, other_common, 'Leur loyer', type_='fixed_amount', amount=10.0)
    assert h.client_a.post(f'/plan/{other_common.id}/envelope/{cat.id}/close').status_code == 404
    # category of another pot under an accessible pot id
    assert h.client_a.post(f'/plan/{h.common.id}/envelope/{cat.id}/close').status_code == 404


def test_personal_plan_of_partner_404(db, household_clean):
    h = household_clean
    resp = h.client_b.post(f'/plan/{h.personal_a.id}/settings', data={'months': '2'})
    assert resp.status_code == 404


def test_common_plan_hides_partner_personal_figures(db, household_clean):
    h = household_clean
    _deficit_setup(db, h, h.common)
    # month already measured by J4 (before J6): its supplement logic stays untouched
    from app.services import refloat_service
    refloat_service.ensure_plan(h.common, current_budget_month(h.common, date.today()), date.today())
    _income(h.personal_a, 4321.0)
    _cat(db, h.personal_a, 'Loyer A', type_='fixed_amount', amount=4300.0)  # cap A = 21
    _income(h.personal_b, 1000.0)
    h.client_b.get('/plan')
    h.client_b.post(f'/plan/{h.common.id}/settings', data={'months': '1', 'split': '100'})
    html = h.client_b.get('/plan').get_data(as_text=True)
    assert 'plafond atteint pour A' in html
    assert '4 321' not in html and '4 300' not in html
    assert '21,00' not in html  # A's capped supplement equals A's cap: hidden from B
    assert '21,00' in h.client_a.get('/plan').get_data(as_text=True)  # A sees it


def test_dashboard_shows_balance_card(db, household_clean):
    h = household_clean
    _account(db, h.common, 987.0)
    _income(h.common, 1000.0)
    html = h.client_a.get('/').get_data(as_text=True)
    assert 'Solde réel' in html and '987,00' in html
    assert 'Argent vraiment libre' in html
    assert '/plan' in html


# --- J6: virements vers le commun -------------------------------------------------------------

def _j6_setup(db, h, salary_a=3000.0, salary_b=2000.0):
    """Common account funded, fixed rent 2400 on the common pot; salaries for the target month."""
    from app.services import contribution_service as cs
    from app.services.budget_service import get_or_create_budget_month
    _account(db, h.common, 0.0, 'Commun')
    _cat(db, h.common, 'Loyer', type_='fixed_amount', amount=2400.0)
    y, m = cs.target_month(h.common, date.today())
    for pot, salary in ((h.personal_a, salary_a), (h.personal_b, salary_b)):
        if salary is not None:
            get_or_create_budget_month(pot.id, y, m).total_income = salary
    db.session.commit()
    return y, m


def test_common_plan_waiting_shows_names_only(db, household_clean, frozen_today):
    h = household_clean
    _j6_setup(db, h, salary_b=None)
    html_a = h.client_a.get(f'/plan').get_data(as_text=True)
    _select(h.client_a, h.common)
    html_a = h.client_a.get('/plan').get_data(as_text=True)
    assert 'en attente du salaire de B' in html_a
    assert '2 000' not in html_a and 'Calculer quand même' in html_a
    _select(h.client_b, h.common)
    html_b = h.client_b.get('/plan').get_data(as_text=True)
    assert 'en attente de votre salaire' in html_b
    assert '3 000' not in html_b and '2 000' not in html_b


def test_force_then_validate_flow(db, household_clean, frozen_today):
    h = household_clean
    _j6_setup(db, h, salary_b=None)
    _select(h.client_a, h.common)
    _select(h.client_b, h.common)
    r = h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'Besoin commun' in html and 'A :' in html and 'en attente de validation' in html
    h.client_a.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
    r = h.client_b.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
    html = r.get_data(as_text=True)
    assert 'Plan validé' in html and 'Rouvrir le plan' in html
    assert 'Plan validé avec un reste à vivre négatif' not in html
    r = h.client_a.post(f'/plan/{h.common.id}/contribution/reopen', headers=HX)
    assert 'Valider le plan' in r.get_data(as_text=True)


def test_allocation_route_rejects_non_provision(db, household_clean, frozen_today):
    h = household_clean
    _j6_setup(db, h)
    rent = Category.query.filter_by(name='Loyer').one()
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    r = h.client_a.post(f'/plan/{h.common.id}/allocation/{rent.id}', data={'amount': '10'}, headers=HX)
    assert r.status_code == 400


def test_contribution_routes_404_on_personal_pot(db, household_clean, frozen_today):
    h = household_clean
    for path in ('contribution/force', 'contribution/validate', 'contribution/reopen'):
        assert h.client_a.post(f'/plan/{h.personal_a.id}/{path}').status_code == 404
    assert h.client_a.post(f'/plan/{h.personal_b.id}/contribution/force').status_code == 404


def test_plan_month_param_limited_to_current_or_next(db, household_clean, frozen_today):
    h = household_clean
    cur = current_budget_month(h.personal_a, date.today())
    y, m = cur.year + (cur.month + 1) // 12, (cur.month + 1) % 12 + 1
    assert h.client_a.get(f'/plan?year={y}&month={m}').status_code == 400
    assert h.client_a.get(f'/plan?year={cur.year}&month={cur.month}').status_code == 200
    r = h.client_a.post(f'/plan/{h.common.id}/contribution/validate', data={'year': y, 'month': m})
    assert r.status_code == 400


def test_personal_plan_shows_my_contribution(db, household_clean, frozen_today):
    h = household_clean
    y, m = _j6_setup(db, h)
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    html = h.client_a.get(f'/plan?year={y}&month={m}').get_data(as_text=True)
    assert 'Besoin commun' in html
    _select(h.client_a, h.personal_a)
    html = h.client_a.get(f'/plan?year={y}&month={m}').get_data(as_text=True)
    assert 'Virement vers le commun : 1 700,00 € (provisoire) — reste à faire 1 700,00 €' in html
    assert '2 000' not in html


def test_daily_job_prepares_plan(app, db, household_clean, frozen_today):
    from app.models import ContributionPlan
    from app.services.alert_service import run_daily_alerts
    h = household_clean
    _j6_setup(db, h)
    assert ContributionPlan.query.count() == 0
    run_daily_alerts(app)
    assert ContributionPlan.query.count() == 1


def test_allocation_slider_and_oob_figures(db, household_clean, frozen_today):
    h = household_clean
    y, m = _j6_setup(db, h)
    _account(db, h.common, 500.0, 'Épargne')
    prov = _cat(db, h.common, 'Voyage', type_='fixed_amount', amount=0.0)
    prov.provision = True
    db.session.commit()
    _select(h.client_a, h.common)
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    page = h.client_a.get('/plan').get_data(as_text=True)
    assert 'name="amount"' in page and 'delay:400ms' in page
    r = h.client_a.post(f'/plan/{h.common.id}/allocation/{prov.id}', data={'amount': '200'}, headers=HX)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'id="contribution-members"' in html and 'hx-swap-oob' in html
    assert 'type="range"' not in html


def test_allocation_allowed_after_validation_shares_frozen(db, household_clean, frozen_today):
    from app.models import ContributionPlan
    from app.services import contribution_service as cs
    h = household_clean
    y, m = _j6_setup(db, h)
    _account(db, h.common, 500.0, 'Épargne')
    prov = _cat(db, h.common, 'Voyage', type_='fixed_amount', amount=0.0)
    prov.provision = True
    db.session.commit()
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    h.client_a.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
    h.client_b.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
    plan = ContributionPlan.query.one()
    assert plan.status == 'validated'
    before = dict(cs.shares(plan))
    r = h.client_a.post(f'/plan/{h.common.id}/allocation/{prov.id}', data={'amount': '200'}, headers=HX)
    assert r.status_code == 200
    db.session.refresh(plan)
    assert plan.allocations[0].amount == 200.0
    assert cs.shares(plan) == before


def test_classify_income_triggers_plan(db, household_clean, frozen_today):
    from app.models import ContributionPlan, Operation
    from app.services import contribution_service as cs
    from app.services.pointage_service import classify
    h = household_clean
    y, m = _j6_setup(db, h, salary_b=None)
    acc = _account(db, h.personal_b, 0.0, 'Perso B')
    day = date(y, m, 15)
    op = Operation(pot_id=h.personal_b.id, bank_account_id=acc.id, user_id=h.user_b.id, amount=2000.0, direction='credit',
                   label='Salaire', bank_label='Salaire', bank_reference='ref1', date=day, bank_date=day)
    db.session.add(op)
    db.session.commit()
    assert ContributionPlan.query.count() == 0
    classify(op, h.user_b, 'income')
    assert ContributionPlan.query.count() == 1
    assert not cs.missing_salaries(h.common, y, m)


def _provision_setup(db, h):
    y, m = _j6_setup(db, h)
    _account(db, h.common, 500.0, 'Épargne')
    prov = _cat(db, h.common, 'Voyage', type_='fixed_amount', amount=0.0)
    prov.provision = True
    db.session.commit()
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    return prov


def test_validate_with_stale_snapshot_is_refused(db, household_clean, frozen_today):
    from app.models import ContributionPlan
    from app.services import contribution_service as cs
    h = household_clean
    prov = _provision_setup(db, h)
    plan = ContributionPlan.query.one()
    old = cs.snapshot(cs.shares(plan))
    h.client_b.post(f'/plan/{h.common.id}/allocation/{prov.id}', data={'amount': '200'}, headers=HX)
    assert cs.snapshot(cs.shares(plan)) != old
    r = h.client_a.post(f'/plan/{h.common.id}/contribution/validate', data={'snapshot': old}, headers=HX)
    assert r.status_code == 409
    html = r.get_data(as_text=True)
    assert 'Le plan a changé, vérifiez les montants avant de valider.' in html and 'Valider le plan' in html
    db.session.refresh(plan)
    assert not cs.is_validated_by(plan, h.user_a)
    new = cs.snapshot(cs.shares(plan))
    r = h.client_a.post(f'/plan/{h.common.id}/contribution/validate', data={'snapshot': new}, headers=HX)
    assert r.status_code == 200 and cs.is_validated_by(plan, h.user_a)


def test_validate_form_posts_snapshot(db, household_clean, frozen_today):
    h = household_clean
    _provision_setup(db, h)
    _select(h.client_a, h.common)
    assert 'hx-vals=' in h.client_a.get('/plan').get_data(as_text=True)


def test_daily_pace_alerts_run_for_j6_common_month(app, db, household_clean, frozen_today, monkeypatch):
    from app.services import alert_service
    h = household_clean
    _provision_setup(db, h)
    seen = []
    monkeypatch.setattr(alert_service, 'check_alerts_for_pot',
                        lambda pot, *a, **kw: seen.append(pot.id) or [])
    alert_service.run_daily_alerts(app)
    assert h.common.id in seen


def test_dashboard_common_j6_month_shows_contributions_as_revenus(db, household_clean, frozen_today):
    from app.models import ContributionPlan
    from app.services import contribution_service as cs
    h = household_clean
    _provision_setup(db, h)
    plan = ContributionPlan.query.one()
    total = sum(cs.shares(plan).values())
    assert total > 0
    _select(h.client_a, h.common)
    html = h.client_a.get(f'/?year={plan.year}&month={plan.month}').get_data(as_text=True)
    expected = f"{total:,.2f}".replace(',', ' ').replace('.', ',')
    assert f'{expected}€' in html
    from app.routes.dashboard import _build_chart_data
    assert _build_chart_data(h.common, plan.year, plan.month, 1)['incomes'] == [pytest.approx(total)]


# --- Simulation du plan à venir -----------------------------------------------------------------

def test_simulation_block_shown_when_no_plan(db, household_clean, frozen_today):
    h = household_clean
    _j6_setup(db, h, salary_b=None)
    _select(h.client_a, h.common)
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert 'Simuler le plan de juin 2026' in html
    assert 'id="simulation"' in html and 'name="salary_%d"' % h.user_b.id in html
    assert 'Estimation basée sur les salaires saisis' in html


def test_simulation_post_saves_and_returns_shares(db, household_clean, frozen_today):
    from app.services import simulation_service as sim
    h = household_clean
    _j6_setup(db, h, salary_b=None)
    get_or_create_budget_month(h.common.id, 2026, 5)   # previous month exists in real life
    r = h.client_a.post(f'/plan/{h.common.id}/simulation', headers=HX, data={
        f'salary_{h.user_a.id}': '3000', f'salary_{h.user_b.id}': '2000'})
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert html.count('devrait virer') == 2 and 'A devrait virer' in html
    # only the result is swapped: the inputs (focus, caret) are not rebuilt
    assert '<input' not in html and 'id="simulation"' not in html
    assert sim.forecasts(h.user_a) == {h.user_a.id: 3000.0, h.user_b.id: 2000.0}
    assert sim.forecasts(h.user_b) == {}
    # empty field deletes the forecast
    h.client_a.post(f'/plan/{h.common.id}/simulation', headers=HX, data={f'salary_{h.user_b.id}': ''})
    assert h.user_b.id not in sim.forecasts(h.user_a)


def test_simulation_non_htmx_post_redirects(db, household_clean, frozen_today):
    h = household_clean
    r = h.client_a.post(f'/plan/{h.common.id}/simulation', data={f'salary_{h.user_a.id}': '10'})
    assert r.status_code == 302


def test_simulation_hidden_once_plan_exists(db, household_clean, frozen_today):
    h = household_clean
    _j6_setup(db, h)
    _select(h.client_a, h.common)
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert 'Simuler le plan de juin 2026' not in html
    assert 'Simuler le plan de juillet 2026' in html   # the next plan stays reachable


@pytest.mark.parametrize('value', ['abc', 'nan', 'inf', '-inf', '-5'])
def test_simulation_rejects_bad_values(db, household_clean, frozen_today, value):
    h = household_clean
    r = h.client_a.post(f'/plan/{h.common.id}/simulation', headers=HX,
                        data={f'salary_{h.user_a.id}': value})
    assert r.status_code == 400


def test_simulation_unknown_member_404(db, household_clean, frozen_today):
    from app.models import Household, User
    h = household_clean
    other = Household(name='Autre')
    db.session.add(other)
    db.session.flush()
    stranger = User(email='x@test.com', display_name='X', household_id=other.id)
    stranger.set_password('Password1!')
    db.session.add(stranger)
    db.session.commit()
    r = h.client_a.post(f'/plan/{h.common.id}/simulation', headers=HX, data={f'salary_{stranger.id}': '10'})
    assert r.status_code == 404
    assert h.client_a.post(f'/plan/{h.common.id}/simulation', headers=HX,
                           data={'salary_zzz': '10'}).status_code == 400


def test_simulation_personal_pot_404(db, household_clean, frozen_today):
    h = household_clean
    assert h.client_a.post(f'/plan/{h.personal_a.id}/simulation', data={}).status_code == 404
    assert h.client_a.post(f'/plan/{h.personal_b.id}/simulation', data={}).status_code == 404


def test_simulation_input_keeps_exact_amount(db, household_clean, frozen_today):
    from app.services import simulation_service as sim
    h = household_clean
    _j6_setup(db, h, salary_b=None)
    _select(h.client_a, h.common)
    sim.save_forecast(h.user_a, h.user_b.id, 12345.67)
    sim.save_forecast(h.user_a, h.user_a.id, 3000.0)
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert 'value="12345.67"' in html and 'value="3000"' in html
    h.client_a.post(f'/plan/{h.common.id}/simulation', headers=HX, data={
        f'salary_{h.user_a.id}': '3000', f'salary_{h.user_b.id}': '12345.67'})
    assert sim.forecasts(h.user_a)[h.user_b.id] == 12345.67


# --- plafonds figés: ceiling slider, personal validate/reopen -------------------------------

def _ceiling_url(pot, cat):
    return f'/plan/{pot.id}/ceiling/{cat.id}'


def test_ceiling_route_updates_and_returns_figures(db, household_clean):
    from app.models import EnvelopeCeiling
    h = household_clean
    courses, culture = _deficit_setup(db, h, h.common)
    page = h.client_a.get('/plan').get_data(as_text=True)
    assert 'name="amount"' in page and 'delay:400ms' in page
    resp = h.client_a.post(_ceiling_url(h.common, culture), data={'amount': '120'}, headers=HX)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '<input type="range"' not in body and 'hx-swap-oob' in body
    row = EnvelopeCeiling.query.filter_by(category_id=culture.id).one()
    assert row.amount == 120.0


def test_ceiling_route_rejects(db, household_clean):
    from app.services import ceiling_service as cs
    h = household_clean
    courses, culture = _deficit_setup(db, h, h.common)
    rent = Category.query.filter_by(pot_id=h.common.id, name='Loyer').one()
    url = _ceiling_url(h.common, culture)
    post = lambda u, **d: h.client_a.post(u, data=d, headers=HX).status_code
    assert post(url, amount='nan') == 400
    assert post(url, amount='inf') == 400
    assert post(url) == 400
    assert post(url, amount='abc') == 400
    assert post(_ceiling_url(h.common, rent), amount='10') == 400   # fixed category
    # negative: clamped to the low bound
    assert post(url, amount='-5') == 200
    env = cs.report(current_budget_month(h.common, date.today())).envelopes[culture.id]
    assert env.amount == env.low
    # closed envelope
    h.client_a.post(f'/plan/{h.common.id}/envelope/{courses.id}/close', headers=HX)
    assert post(_ceiling_url(h.common, courses), amount='300') == 400
    # frozen month
    bm = current_budget_month(h.common, date.today())
    cs.freeze(h.common, bm.year, bm.month)
    assert post(url, amount='50') == 400


def test_ceiling_route_partner_personal_pot_404(db, household_clean):
    h = household_clean
    cat = _cat(db, h.personal_a, 'Loisirs', pct=0.5)
    _income(h.personal_a, 1000.0)
    assert h.client_b.post(_ceiling_url(h.personal_a, cat), data={'amount': '10'},
                           headers=HX).status_code == 404
    assert h.client_b.post(f'/plan/{h.personal_a.id}/ceilings/validate', headers=HX).status_code == 404
    assert h.client_b.post(f'/plan/{h.personal_a.id}/ceilings/reopen', headers=HX).status_code == 404


def test_personal_validate_route_waiting_400_then_ok(db, household_clean, monkeypatch):
    from app.services import ceiling_service as cs
    h = household_clean
    _cat(db, h.personal_a, 'Loisirs', pct=0.5)
    _income(h.personal_a, 1000.0)
    _select(h.client_a, h.personal_a)
    base = f'/plan/{h.personal_a.id}/ceilings'
    monkeypatch.setattr(cs, 'personal_waiting', lambda *a: True)
    assert h.client_a.post(f'{base}/validate', headers=HX).status_code == 400
    monkeypatch.undo()
    resp = h.client_a.post(f'{base}/validate', headers=HX)
    assert resp.status_code == 200
    assert 'id="envelopes-' in resp.get_data(as_text=True)
    bm = current_budget_month(h.personal_a, date.today())
    assert cs.is_frozen(h.personal_a, bm.year, bm.month)
    assert h.client_a.post(f'{base}/validate', headers=HX).status_code == 400   # already frozen
    assert h.client_a.post(f'{base}/reopen', headers=HX).status_code == 200
    assert not cs.is_frozen(h.personal_a, bm.year, bm.month)
    assert h.client_a.post(f'{base}/reopen', headers=HX).status_code == 400     # not frozen


def test_personal_validate_on_common_pot_404(db, household_clean):
    h = household_clean
    for path in ('ceilings/validate', 'ceilings/reopen'):
        assert h.client_a.post(f'/plan/{h.common.id}/{path}', headers=HX).status_code == 404


def test_settings_refused_when_frozen(db, household_clean):
    from app.services import ceiling_service as cs
    h = household_clean
    _deficit_setup(db, h, h.common)
    h.client_a.get('/plan')
    bm = current_budget_month(h.common, date.today())
    cs.freeze(h.common, bm.year, bm.month)
    resp = h.client_a.post(f'/plan/{h.common.id}/settings', headers=HX,
                           data={'months': '3', 'split': '70'})
    assert resp.status_code == 400


# --- one page: ① Commun then ② Mon perso -----------------------------------------------------

def _personal_a_setup(db, h):
    cat = _cat(db, h.personal_a, 'Loisirs A', pct=0.3)
    _income(h.personal_a, 4321.0)   # unallocated = 4321 * 0.7 = 3024.70
    return cat


def test_plan_page_shows_common_then_my_personal(db, household_clean):
    h = household_clean
    _deficit_setup(db, h, h.common)
    _personal_a_setup(db, h)
    _select(h.client_a, h.personal_a)    # the session's pot does not matter any more
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert html.index('① Commun') < html.index('② Mon perso')
    assert 'Loisirs A' in html and f'id="envelopes-{h.personal_a.id}"' in html
    assert f'id="envelopes-{h.common.id}"' in html
    assert f'personal-validation-{h.personal_a.id}' in html


def test_plan_page_partner_never_sees_my_personal(db, household_clean):
    import re
    h = household_clean
    courses, culture = _deficit_setup(db, h, h.common)
    _personal_a_setup(db, h)
    needle = re.compile(r'3\D{0,3}024')

    def clean(html):
        assert 'Loisirs A' not in html and not needle.search(html)
        assert f'personal-validation-{h.personal_a.id}' not in html

    clean(h.client_b.get('/plan').get_data(as_text=True))
    r = h.client_b.post(_ceiling_url(h.common, culture), data={'amount': '120'}, headers=HX)
    assert r.status_code == 200
    clean(r.get_data(as_text=True))
    # A's own response does carry A's personal block
    r = h.client_a.post(_ceiling_url(h.common, culture), data={'amount': '130'}, headers=HX)
    assert 'Loisirs A' in r.get_data(as_text=True)


def test_plan_page_partner_never_sees_my_personal_validation(db, household_clean, frozen_today):
    h = household_clean
    y, m = _j6_setup(db, h)
    _personal_a_setup(db, h)
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    for client, name in ((h.client_a, 'a'), (h.client_b, 'b')):
        r = client.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
        assert r.status_code == 200
        assert ('Loisirs A' in r.get_data(as_text=True)) == (name == 'a')
    r = h.client_b.post(f'/plan/{h.common.id}/contribution/reopen', headers=HX)
    assert 'Loisirs A' not in r.get_data(as_text=True)
    assert 'Loisirs A' not in h.client_b.get('/plan').get_data(as_text=True)


def test_common_slider_response_refreshes_my_personal_oob(db, household_clean):
    h = household_clean
    courses, culture = _deficit_setup(db, h, h.common)
    _personal_a_setup(db, h)
    r = h.client_a.post(_ceiling_url(h.common, culture), data={'amount': '120'}, headers=HX)
    body = r.get_data(as_text=True)
    assert f'personal-validation-{h.personal_a.id}' in body
    assert f'/plan/{h.common.id}/ceiling/' not in body      # common sliders are not re-rendered
    assert f'/plan/{h.personal_a.id}/ceiling/' in body       # the personal ones are (oob)


def test_personal_validate_button_disabled_until_common_validated(db, household_clean, frozen_today):
    h = household_clean
    y, m = _j6_setup(db, h)
    _personal_a_setup(db, h)
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert 'en attente du plan commun' in html
    assert 'Valider mon perso' in html and 'disabled' in html.split('Valider mon perso')[0][-200:]
    h.client_a.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
    h.client_b.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert 'en attente du plan commun' not in html
    assert 'disabled' not in html.split('Valider mon perso')[0][-200:]


def test_full_page_with_validated_common_plan(db, household_clean, frozen_today):
    h = household_clean
    y, m = _j6_setup(db, h)
    _cat(db, h.common, 'Courses', pct=0.25)
    _personal_a_setup(db, h)
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    h.client_a.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
    h.client_b.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert html.index('① Commun') < html.index('ton virement') < html.index('② Mon perso')
    assert '(validé)' in html
    common_sliders = [s for s in html.split('<input') if f'/plan/{h.common.id}/ceiling/' in s]
    assert common_sliders and all('disabled' in s.split('>')[0] for s in common_sliders)
    perso = [s for s in html.split('<input') if f'/plan/{h.personal_a.id}/ceiling/' in s]
    assert perso and not any('disabled' in s.split('>')[0] for s in perso)
    r = h.client_a.post(f'/plan/{h.personal_a.id}/ceilings/validate', headers=HX)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'Plafonds figés pour le mois' in body and 'Rouvrir mon perso' in body


def test_slider_attributes_rounded_to_cents(db, household_clean):
    import re
    h = household_clean
    _account(db, h.personal_a, 500.0)
    _cat(db, h.personal_a, 'Loisirs A', pct=1 / 3, floor=10.004)
    _income(h.personal_a, 1000.0)
    html = h.client_a.get('/plan').get_data(as_text=True)
    tag = next(s for s in html.split('<input') if f'/plan/{h.personal_a.id}/ceiling/' in s)
    for attr in ('min', 'max', 'value'):
        val = re.search(rf'\b{attr}="([^"]*)"', tag).group(1)
        assert re.fullmatch(r'-?\d+(\.\d{1,2})?', val), (attr, val)
    lo, hi, v = (float(re.search(rf'\b{a}="([^"]*)"', tag).group(1)) for a in ('min', 'max', 'value'))
    assert lo <= v <= hi


def test_summary_j6_common_has_no_reportes_line(db, household_clean, frozen_today):
    h = household_clean
    y, m = _j6_setup(db, h)
    _cat(db, h.common, 'Courses', pct=0.5, floor=100.0)
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert '→ reportés' not in html


def test_summary_pre_j6_common_keeps_reportes_line(db, household_clean):
    h = household_clean
    _deficit_setup(db, h, h.common)
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert 'Découvert à résorber' in html and 'Préréglage' in html
    assert 'dont' not in html.split('Découvert à résorber')[1].split('</div>')[0]


# --- review fixes ---------------------------------------------------------------------------

def test_personal_actions_work_when_start_days_differ(db, household_clean, frozen_today):
    h = household_clean
    h.personal_a.income_start_day = 10     # today = 15th: personal window (Jul, Aug) differs from common (Jun, Jul)
    db.session.commit()
    cat = _personal_a_setup(db, h)
    from app.models import Category as C
    page = h.client_a.get('/plan').get_data(as_text=True)
    assert f'/plan/{h.personal_a.id}/ceiling/' in page
    from app.services.budget_service import current_budget_month as cbm
    cm = cbm(h.common, date.today())
    form = {'year': cm.year, 'month': cm.month}
    r = h.client_a.post(_ceiling_url(h.personal_a, cat), data={**form, 'amount': '100'}, headers=HX)
    assert r.status_code == 200
    assert f'personal-validation-{h.personal_a.id}' in r.get_data(as_text=True)   # fix 2
    r = h.client_a.post(f'/plan/{h.personal_a.id}/ceilings/validate', data=form, headers=HX)
    assert r.status_code == 200


def test_common_validation_response_disables_common_sliders(db, household_clean, frozen_today):
    h = household_clean
    _j6_setup(db, h)
    _cat(db, h.common, 'Courses', pct=0.25)
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    h.client_a.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
    body = h.client_b.post(f'/plan/{h.common.id}/contribution/validate', headers=HX).get_data(as_text=True)
    sliders = [s for s in body.split('<input') if f'/plan/{h.common.id}/ceiling/' in s]
    assert sliders and all('disabled' in s.split('>')[0] for s in sliders)


def test_common_slider_response_refreshes_my_transfer_arrow(db, household_clean):
    h = household_clean
    courses, culture = _deficit_setup(db, h, h.common)
    _personal_a_setup(db, h)
    page = h.client_a.get('/plan').get_data(as_text=True)
    assert 'id="my-transfer-arrow"' in page
    r = h.client_a.post(_ceiling_url(h.common, culture), data={'amount': '120'}, headers=HX)
    body = r.get_data(as_text=True)
    assert 'id="my-transfer-arrow"' in body and 'hx-swap-oob' in body
    # B's response carries B's own arrow only, never A's
    rb = h.client_b.post(_ceiling_url(h.common, culture), data={'amount': '121'}, headers=HX)
    assert 'Loisirs A' not in rb.get_data(as_text=True)


def test_j6_slider_and_validation_refresh_arrow_amount(db, household_clean, frozen_today):
    import re
    h = household_clean
    y, m = _j6_setup(db, h)
    loisirs = _cat(db, h.common, 'Loisirs', pct=0.1)
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)

    def arrow(body):
        found = re.search(r'id="my-transfer-arrow"[^>]*>([^<]*)<', body)
        assert found, body
        return found.group(1)

    page = arrow(h.client_a.get('/plan').get_data(as_text=True))
    assert 'provisoire' in page
    r = h.client_a.post(_ceiling_url(h.common, loisirs), data={'amount': '0'}, headers=HX)
    moved = arrow(r.get_data(as_text=True))
    assert moved != page and 'provisoire' in moved
    assert arrow(h.client_a.get('/plan').get_data(as_text=True)) == moved
    r = h.client_a.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
    assert 'provisoire' in arrow(r.get_data(as_text=True))
    r = h.client_b.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
    assert 'validé' in arrow(h.client_a.get('/plan').get_data(as_text=True))


# --- barre collante des virements proposés (montants entiers) -------------------------------

def _j6_common(db, h, frozen_today):
    y, m = _j6_setup(db, h)
    _cat(db, h.common, 'Courses', pct=0.25)
    _personal_a_setup(db, h)
    h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX)
    return y, m


def test_transfers_bar_on_plan_for_both_members(db, household_clean, frozen_today):
    h = household_clean
    y, m = _j6_common(db, h, frozen_today)
    plan = cs.get_plan(h.common, y, m)
    assert plan is not None
    amounts = cs.shares(plan)
    assert all(v == int(v) for v in amounts.values())
    for client in (h.client_a, h.client_b):
        html = client.get('/plan').get_data(as_text=True)
        assert 'id="transfers-bar-' in html
        bar = html[html.index('id="transfers-bar-'):]
        bar = bar[:bar.index('</div>')]
        assert 'provisoire' in bar
        for u in (h.user_a, h.user_b):
            assert u.display_name in bar
            assert f'{int(amounts[u.id]):,}'.replace(',', ' ') + ' €' in bar
        # in the common section, before section ②
        assert html.index('id="transfers-bar-') < html.index('② Mon perso')


def test_transfers_bar_absent_without_plan(db, household_clean):
    h = household_clean
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert 'Virements proposés' not in html


def test_transfers_bar_refreshed_oob_in_ceiling_response_and_validated(db, household_clean, frozen_today):
    h = household_clean
    _j6_common(db, h, frozen_today)
    cat = Category.query.filter_by(pot_id=h.common.id, name='Courses').one()
    body = h.client_a.post(_ceiling_url(h.common, cat), data={'amount': '300'}, headers=HX).get_data(as_text=True)
    m = re.search(r'<div[^>]*id="transfers-bar-\d+"[^>]*>', body)
    assert m and 'hx-swap-oob' in m.group(0)
    h.client_a.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)
    r = h.client_b.post(f'/plan/{h.common.id}/contribution/validate', headers=HX).get_data(as_text=True)
    assert 'id="transfers-bar-' in r and 'validé' in r[r.index('id="transfers-bar-'):][:600]


def test_transfers_bar_has_no_personal_figures(db, household_clean, frozen_today):
    h = household_clean
    _j6_common(db, h, frozen_today)
    html = h.client_b.get('/plan').get_data(as_text=True)
    bar = html[html.index('id="transfers-bar-'):]
    bar = bar[:bar.index('</div>')]
    assert '4321' not in bar and '4 321' not in bar and 'Loisirs A' not in bar
