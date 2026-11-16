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

