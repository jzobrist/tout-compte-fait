from datetime import date

import pytest

from app.models import BudgetMonth, ContributionPlan
from app.services import nav_service
from app.services.nav_service import plan_pending
from tests.test_plan_routes import HX, _j6_setup


@pytest.fixture
def nav_today(frozen_today, monkeypatch):
    """frozen_today, also seen by nav_service (context processor runs outside the plan routes)."""
    from tests import test_plan_routes
    monkeypatch.setattr(nav_service, 'date', type(frozen_today))
    monkeypatch.setattr(test_plan_routes, 'date', type(frozen_today))   # _j6_setup reads its own date
    return frozen_today


def _force(h):
    assert h.client_a.post(f'/plan/{h.common.id}/contribution/force', headers=HX).status_code == 200


def _validate_common(client, h):
    client.post(f'/plan/{h.common.id}/contribution/validate', headers=HX)


def test_no_common_plan_yet_not_pending(db, household_clean, nav_today):
    h = household_clean
    assert plan_pending(h.user_a, date.today()) is False


def test_common_plan_to_validate_pending_for_both(db, household_clean, nav_today):
    h = household_clean
    _j6_setup(db, h)
    _force(h)
    assert plan_pending(h.user_a, date.today()) is True
    assert plan_pending(h.user_b, date.today()) is True


def test_validated_by_me_only_not_pending_for_me(db, household_clean, nav_today):
    h = household_clean
    _j6_setup(db, h)
    _force(h)
    _validate_common(h.client_a, h)
    assert plan_pending(h.user_a, date.today()) is False
    assert plan_pending(h.user_b, date.today()) is True


def test_common_validated_then_personal_pending_until_frozen(db, household_clean, nav_today):
    h = household_clean
    _j6_setup(db, h)
    _force(h)
    _validate_common(h.client_a, h)
    _validate_common(h.client_b, h)
    assert plan_pending(h.user_a, date.today()) is True      # perso still open
    r = h.client_a.post(f'/plan/{h.personal_a.id}/ceilings/validate', headers=HX)
    assert r.status_code == 200
    assert plan_pending(h.user_a, date.today()) is False
    assert plan_pending(h.user_b, date.today()) is True


def test_reopen_common_makes_pending_again(db, household_clean, nav_today):
    h = household_clean
    _j6_setup(db, h)
    _force(h)
    _validate_common(h.client_a, h)
    _validate_common(h.client_b, h)
    h.client_a.post(f'/plan/{h.personal_a.id}/ceilings/validate', headers=HX)
    h.client_b.post(f'/plan/{h.personal_b.id}/ceilings/validate', headers=HX)
    assert plan_pending(h.user_a, date.today()) is False
    assert plan_pending(h.user_b, date.today()) is False
    h.client_a.post(f'/plan/{h.common.id}/contribution/reopen', headers=HX)
    assert plan_pending(h.user_a, date.today()) is True
    assert plan_pending(h.user_b, date.today()) is True


def test_single_member_household_personal_pending(db, app):
    from app.models import Pot, User
    client = app.test_client()
    client.post('/register', data={'email': 'solo@test.com', 'display_name': 'Solo',
                                   'password': 'Password1!', 'household_name': 'Solo'})
    user = User.query.filter_by(email='solo@test.com').one()
    personal = Pot.query.filter_by(owner_id=user.id, kind='personal').one()
    assert plan_pending(user, date.today()) is True
    assert client.post(f'/plan/{personal.id}/ceilings/validate', headers=HX).status_code == 200
    assert plan_pending(user, date.today()) is False


def test_user_without_personal_pot_or_common_pot(db, household_clean, nav_today):
    from app.models import Pot
    h = household_clean
    Pot.query.filter_by(id=h.common.id).update({'kind': 'other'})
    db.session.commit()
    assert plan_pending(h.user_a, date.today()) is True      # perso open, nothing to wait for
    Pot.query.filter_by(id=h.personal_a.id).update({'owner_id': None})
    db.session.commit()
    assert plan_pending(h.user_a, date.today()) is False


def test_does_not_write(db, household_clean, nav_today):
    h = household_clean
    _j6_setup(db, h)
    before = (BudgetMonth.query.count(), ContributionPlan.query.count())
    plan_pending(h.user_a, date.today())
    plan_pending(h.user_b, date.today())
    db.session.commit()
    assert (BudgetMonth.query.count(), ContributionPlan.query.count()) == before


def test_default_today(db, household_clean, nav_today):
    h = household_clean
    _j6_setup(db, h)
    _force(h)
    assert plan_pending(h.user_a) is True



# --- navbar rendering -------------------------------------------------------

def test_navbar_links_and_menu(db, household_clean):
    html = household_clean.client_a.get('/').get_data(as_text=True)
    assert 'Tableau de bord' in html
    assert 'class="dropdown-menu' in html
    for label in ('Plan du mois', 'Revenus', 'Paramètres', 'Déconnexion'):
        assert label in html
    assert 'id="current-pot-name"' not in html          # no duplicate pot name


def test_active_link_dashboard(db, household_clean):
    import re
    html = household_clean.client_a.get('/').get_data(as_text=True)
    assert re.search(r'class="nav-link active"[^>]*href="/"', html) or \
        re.search(r'href="/"[^>]*class="nav-link active"', html)
    assert not re.search(r'class="nav-link active"[^>]*href="/history', html)


def test_pending_button_and_mobile_alert(db, household_clean, nav_today):
    h = household_clean
    _j6_setup(db, h)
    _force(h)
    html = h.client_a.get('/').get_data(as_text=True)
    assert 'Plan à valider' in html and 'id="plan-pending-alert"' in html
    html = h.client_a.get('/plan').get_data(as_text=True)
    assert 'Plan à valider' in html and 'id="plan-pending-alert"' not in html


def test_no_button_when_nothing_to_do(db, household_clean, nav_today):
    html = household_clean.client_a.get('/').get_data(as_text=True)
    assert 'Plan à valider' not in html and 'plan-pending-alert' not in html


def test_htmx_fragment_does_not_compute_pending(db, household_clean, nav_today, monkeypatch):
    calls = []
    monkeypatch.setattr(nav_service, 'plan_pending', lambda *a, **k: calls.append(1) or False)
    household_clean.client_a.get('/operations/pointage', headers=HX)
    assert calls == []
    household_clean.client_a.get('/')
    assert calls == [1]                                   # computed once, cached for the page


def test_gear_menu_shared_by_desktop_and_mobile(db, household_clean):
    html = household_clean.client_a.get('/accounts').get_data(as_text=True)
    bottom = html[html.index('<nav class="bottom-nav">'):]
    bottom = bottom[:bottom.index('</nav>')]
    assert bottom.count('bottom-nav-item') == 4                     # Dashboard, Historique | Pointage, ⚙️
    assert 'bottom-nav-fab' in bottom and 'settings-menu' in bottom
    assert html.count('class="dropdown-menu dropdown-menu-end settings-menu"') == 2
    assert html.count('>Comptes</a>') == 2                           # only in the two ⚙️ menus
    assert 'id="theme-toggle"' not in html and html.count('dropdown-item theme-toggle') == 2
