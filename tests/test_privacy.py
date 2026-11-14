import datetime

import pytest

from app.extensions import db as _db
from app.models import Category
from app.models.adjustment import BudgetAdjustment
from app.models.income import Income
from app.models.operation import Operation


@pytest.fixture
def secret(db, household_ab):
    """Objects living in A's personal pot."""
    h = household_ab
    cat = Category(name='Secret A', type='fixed_amount', amount=50,
                   pot_id=h.personal_a.id)
    _db.session.add(cat)
    _db.session.flush()
    op = Operation(pot_id=h.personal_a.id,
                   user_id=h.user_a.id, category_id=cat.id, amount=5,
                   label='Depense secrete', date=datetime.date.today())
    pending = Operation(pot_id=h.personal_a.id,
                        user_id=h.user_a.id, amount=7, label='Import secret',
                        date=datetime.date.today(), bank_reference='SECRET1')
    inc = Income(pot_id=h.personal_a.id,
                 user_id=h.user_a.id, amount=100, label='Salaire secret',
                 date=datetime.date.today())
    _db.session.add_all([op, pending, inc])
    _db.session.commit()
    return {'cat': cat, 'op': op, 'pending': pending, 'inc': inc}


@pytest.mark.parametrize('method,url', [
    ('get', '/categories/{cat}/edit'), ('post', '/categories/{cat}/delete'),
    ('get', '/operations/{op}/edit'), ('post', '/operations/{op}/delete'),
    ('post', '/operations/{pending}/classify'), ('post', '/incomes/{inc}/delete'),
    ('post', '/pots/{pot}/select'),
])
def test_b_gets_404_on_a_personal_objects(household_ab, secret, method, url):
    h = household_ab
    url = url.format(cat=secret['cat'].id, op=secret['op'].id, pending=secret['pending'].id,
                     inc=secret['inc'].id, pot=h.personal_a.id)
    data = ({'classification': 'category', 'category_id': h.common.categories.first().id}
            if 'classify' in url else {})
    resp = getattr(h.client_b, method)(url, data=data)
    assert resp.status_code == 404
    assert _db.session.get(Category, secret['cat'].id) is not None
    assert _db.session.get(Operation, secret['op'].id) is not None
    assert _db.session.get(Income, secret['inc'].id) is not None
    assert _db.session.get(Operation, secret['pending'].id).category_id is None


def test_b_dashboard_and_lists_never_show_a_personal_data(household_ab, secret):
    h = household_ab
    for select in (h.common, h.personal_b):
        h.client_b.post(f'/pots/{select.id}/select')
        for url in ('/', '/categories', '/history', '/operations/pointage', '/incomes',
                    '/operations', '/operations/form', '/adjustments/form'):
            html = h.client_b.get(url).get_data(as_text=True)
            for needle in ('Secret A', 'Depense secrete', 'Import secret', 'Salaire secret'):
                assert needle not in html, (url, needle)


def test_a_sees_personal_data_only_on_personal_pot(household_ab, secret):
    h = household_ab
    # category pages list every accessible family whatever the displayed pot (B never sees it: see above)
    assert 'Secret A' in h.client_a.get('/categories').get_data(as_text=True)
    h.client_a.post(f'/pots/{h.personal_a.id}/select')
    assert 'Secret A' in h.client_a.get('/categories').get_data(as_text=True)
    assert 'Import secret' in h.client_a.get('/operations/pointage').get_data(as_text=True)


def test_dashboard_alert_banner_and_chart_reflect_current_pot_only(household_ab, secret):
    h = household_ab
    # heavy spending in A's personal pot must not leak into the common dashboard
    big = Operation(pot_id=h.personal_a.id,
                    user_id=h.user_a.id, category_id=secret['cat'].id, amount=987654,
                    label='Gros', date=datetime.date.today())
    _db.session.add(big)
    _db.session.commit()
    html = h.client_a.get('/').get_data(as_text=True)
    assert '987654' not in html and '987 654' not in html
    assert 'Secret A' not in html


def test_b_can_edit_common_category(household_ab):
    h = household_ab
    cat = h.common.categories.first()
    assert h.client_b.get(f'/categories/{cat.id}/edit').status_code == 200


def test_operation_goes_to_category_pot_not_session_pot(household_ab):
    h = household_ab
    h.client_a.post(f'/pots/{h.personal_a.id}/select')
    cat = h.common.categories.first()
    resp = h.client_a.post('/operations', data={
        'amount': '12', 'category_id': cat.id, 'label': 'X',
        'date': datetime.date.today().isoformat(),
    })
    assert resp.status_code in (200, 302)
    op = Operation.query.filter_by(label='X').one()
    assert op.pot_id == h.common.id


def test_b_cannot_use_a_personal_category_in_forms(household_ab, secret):
    h = household_ab
    data = {'amount': '12', 'category_id': secret['cat'].id, 'label': 'Intrus',
            'date': datetime.date.today().isoformat()}
    resp = h.client_b.post('/operations', data=data)
    assert resp.status_code == 200  # invalid choice: form re-rendered
    assert 'Secret A' not in resp.get_data(as_text=True)
    resp = h.client_b.post('/operations', data=data, headers={'HX-Request': 'true'})
    assert resp.status_code == 422
    assert 'Secret A' not in resp.get_data(as_text=True)
    assert Operation.query.filter_by(label='Intrus').count() == 0
    other = h.common.categories.first()
    for frm, to in ((secret['cat'].id, other.id), (other.id, secret['cat'].id)):
        resp = h.client_b.post('/adjustments', data={
            'from_category_id': frm, 'to_category_id': to, 'amount': '5'})
        assert resp.status_code == 302  # invalid choice: silently redirected, nothing created
        assert 'Secret A' not in resp.get_data(as_text=True)
    assert BudgetAdjustment.query.count() == 0


def test_b_invalid_operation_form_never_lists_foreign_categories(household_ab, secret):
    h = household_ab
    for headers in ({}, {'HX-Request': 'true'}):
        resp = h.client_b.post('/operations', data={'amount': '', 'category_id': ''}, headers=headers)
        assert resp.status_code in (200, 422)
        assert 'Secret A' not in resp.get_data(as_text=True)


def test_b_cannot_classify_into_a_personal_category(household_ab, secret):
    h = household_ab
    own = Operation(pot_id=h.common.id,
                    user_id=h.user_b.id, amount=3, label='B op',
                    date=datetime.date.today(), bank_reference='B1')
    _db.session.add(own)
    _db.session.commit()
    resp = h.client_b.post(f'/operations/{own.id}/classify',
                           data={'classification': 'category', 'category_id': secret['cat'].id})
    assert resp.status_code == 404
    assert _db.session.get(Operation, own.id).category_id is None


def test_classify_into_other_pot_category_is_400(household_ab, secret):
    h = household_ab
    own = Operation(pot_id=h.common.id,
                    user_id=h.user_a.id, amount=3, label='A op',
                    date=datetime.date.today(), bank_reference='A1')
    _db.session.add(own)
    _db.session.commit()
    resp = h.client_a.post(f'/operations/{own.id}/classify',
                           data={'classification': 'category', 'category_id': secret['cat'].id})
    assert resp.status_code == 400
    assert _db.session.get(Operation, own.id).category_id is None


@pytest.mark.parametrize('endpoint,extra', [
    ('/incomes', {'amount': '10', 'label': 'Prime'}),
])
def test_write_with_foreign_pot_id_is_404(household_ab, endpoint, extra):
    h = household_ab
    resp = h.client_b.post(endpoint, data={**extra, 'pot_id': h.personal_a.id})
    assert resp.status_code == 404
    assert Income.query.count() == 0
    assert Category.query.filter_by(name='Intrus').count() == 0


def test_category_create_ignores_forged_pot_id(household_ab):
    h = household_ab
    resp = h.client_b.post('/categories', data={
        'name': 'Intrus', 'type': 'fixed_amount', 'common-amount': '5', 'personal': 'own',
        'pot_id': h.personal_a.id})
    assert resp.status_code == 302
    assert [c.pot_id for c in Category.query.filter_by(name='Intrus')] == [h.personal_b.id]


def test_write_with_own_pot_id_uses_that_pot_not_session(household_ab):
    h = household_ab
    h.client_a.post(f'/pots/{h.personal_a.id}/select')
    h.client_a.post('/incomes', data={'amount': '10', 'label': 'Prime', 'pot_id': h.common.id})
    assert Income.query.one().pot_id == h.common.id
    h.client_a.post('/categories', data={'name': 'Neuve', 'type': 'fixed_amount', 'common': 'y',
                                         'common-amount': '5', 'pot_id': h.common.id})
    assert Category.query.filter_by(name='Neuve').one().pot_id == h.common.id


def test_switcher_shows_current_pot_name(household_ab):
    h = household_ab
    assert 'Commun' in h.client_a.get('/').get_data(as_text=True)
    resp = h.client_a.post(f'/pots/{h.personal_a.id}/select', headers={'Referer': 'http://localhost/categories'})
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/categories')
    html = h.client_a.get('/').get_data(as_text=True)
    assert 'Perso – A' in html
    assert 'Mon perso' in html


def test_select_redirects_to_dashboard_for_foreign_referrer(household_ab):
    h = household_ab
    resp = h.client_a.post(f'/pots/{h.common.id}/select', headers={'Referer': 'http://evil.example/x'})
    assert resp.status_code == 302
    assert 'evil.example' not in resp.headers['Location']


def test_adjustment_across_pots_is_400(household_ab, secret):
    h = household_ab
    common_cat = h.common.categories.first()
    resp = h.client_a.post('/adjustments', data={
        'from_category_id': common_cat.id, 'to_category_id': secret['cat'].id, 'amount': '5'})
    assert resp.status_code == 400


# --- J6: the common plan never reveals a member's personal pot ----------------------------------
HX = {'HX-Request': 'true'}
SALARY_A, SALARY_B = 3123.45, 2087.65
CHARGE_A, CHARGE_B = 411.11, 233.33
BASE_A, BASE_B = round(SALARY_A - CHARGE_A, 2), round(SALARY_B - CHARGE_B, 2)
# every way the money filter / browsers may render a space inside a number
SPACES = (' ', '\u202f', '\xa0', '&nbsp;', '')


def _variants(value):
    """Raw and money-formatted renderings of a number, with every space variant."""
    out = {f'{value:.2f}', f'{value:.2f}'.replace('.', ','), str(int(value))}
    whole, cents = f'{value:.2f}'.split('.')
    if len(whole) > 3:
        for sp in SPACES:
            out.add(f'{whole[:-3]}{sp}{whole[-3:]}')
            out.add(f'{whole[:-3]}{sp}{whole[-3:]},{cents}')
    return out


def _j6_secret_household(h):
    from app.models import BankAccount
    from app.services import contribution_service as cs
    from app.services.budget_service import get_or_create_budget_month
    _db.session.add(BankAccount(pot_id=h.common.id, name='Commun', bank_name='B', balance_booked=0.0,
                                balance_value=0.0, balance_at=datetime.datetime(2026, 6, 15, 6, 0)))
    _db.session.add_all([
        Category(name='Loyer', type='fixed_amount', amount=2400.0, pot_id=h.common.id, carry_over=False),
        # 25% only: need < sum of bases, so the equal reste a vivre R is > 0
        Category(name='Courses', type='dynamic', percentage=0.25, pot_id=h.common.id, carry_over=False),
        Category(name='Prêt secret', type='fixed_amount', amount=CHARGE_A, constrained=True,
                 pot_id=h.personal_a.id, carry_over=False),
        Category(name='Crédit auto B', type='fixed_amount', amount=CHARGE_B, constrained=True,
                 pot_id=h.personal_b.id, carry_over=False),
    ])
    _db.session.commit()
    y, m = cs.target_month(h.common, datetime.date(2026, 6, 15))
    get_or_create_budget_month(h.personal_a.id, y, m).total_income = SALARY_A
    get_or_create_budget_month(h.personal_b.id, y, m).total_income = SALARY_B
    _db.session.commit()
    return y, m


BAL_B, OVERDRAFT_B = -1234.56, 654.32


def add_capped_b(db, h, extra_overdraft):
    """B's personal accounts: a distinctive overdrawn balance and overdraft limits; returns B's cap."""
    from app.models import BankAccount
    at = datetime.datetime(2026, 6, 15, 6, 0)
    db.session.add_all([
        BankAccount(pot_id=h.personal_b.id, name='Perso B', bank_name='B', balance_value=BAL_B,
                    balance_booked=BAL_B, balance_at=at, overdraft_limit=OVERDRAFT_B),
        BankAccount(pot_id=h.personal_b.id, name='Réserve B', bank_name='B', balance_value=0.0,
                    balance_booked=0.0, balance_at=at, overdraft_limit=extra_overdraft)])
    db.session.commit()
    return round(BAL_B - CHARGE_B + OVERDRAFT_B + extra_overdraft, 2)


def _forbidden(h, plan_shares, own):
    """Strings the user `own` ('a' or 'b') must never see: the OTHER member's pot figures."""
    other_salary, other_charge, other_base, other_name = (
        (SALARY_B, CHARGE_B, BASE_B, 'Crédit auto B') if own == 'a'
        else (SALARY_A, CHARGE_A, BASE_A, 'Prêt secret'))
    other_id = h.user_b.id if own == 'a' else h.user_a.id
    own_id = h.user_a.id if own == 'a' else h.user_b.id
    own_base = BASE_A if own == 'a' else BASE_B
    r = round(own_base - plan_shares[own_id], 2)
    assert r > 0
    forbidden = _variants(other_salary) | _variants(other_charge) | _variants(other_base)
    forbidden |= _variants(r) | {other_name, other_name.replace('é', 'e')}
    return forbidden


def _assert_clean(html, where, forbidden):
    for needle in forbidden:
        assert needle not in html, (where, needle)


def test_common_plan_never_shows_partner_salary(db, household_clean, frozen_today):
    from app.models import Category as Cat
    from app.services import contribution_service as cs
    h = household_clean
    y, m = _j6_secret_household(h)
    h.client_b.post(f'/pots/{h.common.id}/select')
    h.client_a.post(f'/pots/{h.common.id}/select')
    base = f'/plan/{h.common.id}'
    courses = Cat.query.filter_by(name='Courses', pot_id=h.common.id).one()
    loyer = Cat.query.filter_by(name='Loyer', pot_id=h.common.id).one()

    pages = {}   # name -> (html, status, expected marker, viewer)
    def grab(name, resp, marker, viewer='b'):
        pages[name] = (resp.get_data(as_text=True), resp.status_code, marker, viewer)

    grab('plan', h.client_b.get('/plan'), 'Plan du mois')
    grab('plan_month', h.client_b.get(f'/plan?year={y}&month={m}'), 'Plan du mois')
    grab('force', h.client_b.post(f'{base}/contribution/force', headers=HX), 'Besoin commun')
    plan = cs.get_plan(h.common, y, m)
    live = cs.shares(plan)
    assert live[h.user_a.id] != BASE_A and live[h.user_b.id] != BASE_B
    grab('force_a', h.client_a.get('/plan'), 'Besoin commun', 'a')
    grab('settings', h.client_b.post(f'{base}/settings', data={'months': '1', 'split': '50'}, headers=HX), '')
    r = h.client_b.post(f'{base}/allocation/{loyer.id}', data={'amount': '10'}, headers=HX)
    assert r.status_code == 400
    grab('validate_b', h.client_b.post(f'{base}/contribution/validate', headers=HX), 'Besoin commun')
    grab('validate_a', h.client_a.post(f'{base}/contribution/validate', headers=HX), 'Plan validé', 'a')
    amounts = {u: cs.shares(cs.get_plan(h.common, y, m))[u] for u in (h.user_a.id, h.user_b.id)}
    grab('validated', h.client_b.get('/plan'), 'Plan validé')
    html_validated = pages['validated'][0]
    for uid, amount in amounts.items():
        assert any(v in html_validated for v in
                   (f"{amount:,.2f}".replace(',', ' ').replace('.', ','),
                    f"{amount:,.2f}".replace(',', '\u202f').replace('.', ','))), amount
    grab('reopen', h.client_b.post(f'{base}/contribution/reopen', headers=HX), 'Valider le plan')
    grab('reopen_a', h.client_a.get('/plan'), 'Valider le plan', 'a')
    grab('validate_a2', h.client_a.post(f'{base}/contribution/validate', headers=HX), 'Besoin commun', 'a')
    grab('validate_b2', h.client_b.post(f'{base}/contribution/validate', headers=HX), 'Plan validé')
    for url in ('/', '/incomes', '/history', '/operations/pointage', '/categories'):
        grab(url, h.client_b.get(url), '')
        grab(url + '#a', h.client_a.get(url), '', 'a')
    for tab in ('a-classer', 'rapproches', 'en-attente', 'tous'):
        grab('tab_' + tab, h.client_b.get(f'/operations/pointage?tab={tab}'), '')
        grab('tabhx_' + tab, h.client_b.get(f'/operations/pointage?tab={tab}', headers=HX), '')
        grab('tab_a_' + tab, h.client_a.get(f'/operations/pointage?tab={tab}', headers=HX), '', 'a')
    forb = {v: _forbidden(h, amounts, v) for v in ('a', 'b')}
    for where, (html, status, marker, viewer) in pages.items():
        if where == 'settings':   # no J4 deficit plan here: 400 is expected, still scanned
            assert status in (200, 400), where
        else:
            assert status == 200, where
            assert html.strip() and marker in html, (where, marker)
        _assert_clean(html, where, forb[viewer])


def test_validation_mail_has_no_salary(db, household_clean, frozen_today, monkeypatch):
    from app.services import alert_service, contribution_service as cs
    sent = []
    monkeypatch.setattr(alert_service, 'send_alert_email',
                        lambda to, subject, body: sent.append((tuple(to), subject, body)))
    h = household_clean
    y, m = _j6_secret_household(h)
    base = f'/plan/{h.common.id}'
    h.client_a.post(f'/pots/{h.common.id}/select')
    h.client_b.post(f'/pots/{h.common.id}/select')
    h.client_a.post(f'{base}/contribution/force', headers=HX)
    h.client_a.post(f'{base}/contribution/validate', headers=HX)
    h.client_b.post(f'{base}/contribution/validate', headers=HX)
    assert sent
    shares = cs.shares(cs.get_plan(h.common, y, m))
    forbidden = _forbidden(h, shares, 'a') | _forbidden(h, shares, 'b')
    for to, subject, body in sent:
        _assert_clean(subject + body, 'mail', forbidden)
    body = sent[0][2]
    assert 'A' in body and 'B' in body
    for amount in shares.values():
        assert f'{amount:.2f} €' in body


def test_category_pages_never_show_partner_amounts(household_ab):
    h = household_ab
    h.client_a.post('/categories', data={
        'name': 'Loisirs', 'type': 'fixed_amount', 'common': 'y', 'personal': 'shared',
        'common-amount': '10', 'personal-amount': '10'})
    shared_b = Category.query.filter_by(pot_id=h.personal_b.id, name='Loisirs').one()
    shared_b.amount = 777
    _db.session.add(Category(name='Secret B', type='fixed_amount', amount=5,
                             pot_id=h.personal_b.id, scope='own'))
    _db.session.commit()
    cat = Category.query.filter_by(pot_id=h.personal_a.id, name='Loisirs').one()
    for url in ('/categories', f'/categories/{cat.id}/edit'):
        html = h.client_a.get(url).get_data(as_text=True)
        assert '777' not in html and 'Secret B' not in html, url


@pytest.mark.parametrize('extra_overdraft,binding', [(2000.0, False), (1000.0, True)])
def test_plan_page_never_shows_partner_cap_or_balance(db, household_clean, frozen_today, monkeypatch,
                                                      extra_overdraft, binding):
    from app.services import alert_service, contribution_service as cs
    sent = []
    monkeypatch.setattr(alert_service, 'send_alert_email',
                        lambda to, subject, body: sent.append(subject + body))
    h = household_clean
    y, m = _j6_secret_household(h)
    cap = add_capped_b(db, h, extra_overdraft)
    base = f'/plan/{h.common.id}'
    for c in (h.client_a, h.client_b):
        c.post(f'/pots/{h.common.id}/select')
    pages = [h.client_a.post(f'{base}/contribution/force', headers=HX).get_data(as_text=True)]
    plan = cs.get_plan(h.common, y, m)
    assert cs.adjusted(plan) is binding
    forbidden = _variants(-BAL_B) | _variants(OVERDRAFT_B) | _variants(BAL_B) | {'Crédit auto B'}
    if not binding:   # when a cap binds, B's share (public) equals it
        forbidden |= _variants(cap)
    pages.append(h.client_a.get('/plan').get_data(as_text=True))
    pages.append(h.client_a.post(f'{base}/contribution/validate', headers=HX).get_data(as_text=True))
    h.client_b.post(f'{base}/contribution/validate', headers=HX)
    pages.append(h.client_a.get('/plan').get_data(as_text=True))
    assert sent
    pages += sent
    for i, html in enumerate(pages):
        _assert_clean(html, i, forbidden)


def test_simulation_never_shows_partner_figures(db, household_clean, frozen_today):
    from app.models import BankAccount
    from app.services import simulation_service as sim
    from app.services.budget_service import get_or_create_budget_month
    h = household_clean
    _db.session.add(BankAccount(pot_id=h.common.id, name='Commun', bank_name='B', balance_booked=0.0,
                                balance_value=0.0, balance_at=datetime.datetime(2026, 6, 15, 6, 0)))
    _db.session.add_all([
        Category(name='Loyer', type='fixed_amount', amount=2400.0, pot_id=h.common.id, carry_over=False),
        Category(name='Crédit secret', type='fixed_amount', amount=432.10, constrained=True,
                 pot_id=h.personal_b.id, carry_over=False),
        BankAccount(pot_id=h.personal_b.id, name='Perso B', bank_name='B', balance_value=-1234.56,
                    balance_booked=-1234.56, balance_at=datetime.datetime(2026, 6, 15, 6, 0),
                    overdraft_limit=987.65)])
    _db.session.commit()
    get_or_create_budget_month(h.personal_b.id, 2026, 6).total_income = 2345.67
    get_or_create_budget_month(h.common.id, 2026, 5)
    _db.session.commit()
    forbidden = _variants(2345.67) | _variants(432.10) | _variants(1234.56) | _variants(987.65) \
        | {'Crédit secret', 'Credit secret'}
    h.client_a.post(f'/pots/{h.common.id}/select')
    h.client_b.post(f'/pots/{h.common.id}/select')
    r = h.client_a.post(f'/plan/{h.common.id}/simulation', headers=HX, data={
        f'salary_{h.user_a.id}': '3000', f'salary_{h.user_b.id}': '1111'})
    post_html = r.get_data(as_text=True)
    assert 'devrait virer' in post_html
    page_a = h.client_a.get('/plan').get_data(as_text=True)
    assert 'Simuler le plan de' in page_a
    _assert_clean(post_html, 'post', forbidden)
    _assert_clean(page_a, 'page', forbidden)
    # A's typed salary for B stays with A
    assert sim.forecasts(h.user_b) == {}
    page_b = h.client_b.get('/plan').get_data(as_text=True)
    assert '1111' not in page_b and '1 111' not in page_b
