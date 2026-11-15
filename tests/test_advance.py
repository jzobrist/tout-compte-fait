"""Common fixed charge paid from a member's personal account (« payé depuis le compte perso de »)."""
from datetime import date

import pytest

from app.extensions import db as _db
from app.models import Category, User
from app.services import category_service
from app.services import contribution_service as cs
from app.services.category_service import CategoryError
from tests.test_contribution_service import (  # noqa: F401
    M, TODAY, _account, _ccat, _ready, _salary, mail_outbox,
)


def _settings(cat, paid_by=None, amount=None):
    return {'common': {'amount': cat.amount if amount is None else amount, 'carry_over': False,
                       'paid_by': paid_by}}


def _set(user, cat, paid_by=None, amount=None, type_=None):
    category_service.update_family(user, cat, cat.name, type_ or cat.type, True, None,
                                   _settings(cat, paid_by, amount))


def _pret(db, h, amount=800.0):
    return _ccat(db, h.common, 'Prêt', 'fixed_auto', amount=amount)


@pytest.fixture
def today(monkeypatch):
    """advance_locked reads date.today(): pin it to TODAY (2026-11-15)."""
    import app.services.category_service as mod

    class _D(date):
        @classmethod
        def today(cls):
            return TODAY
    monkeypatch.setattr(mod, 'date', _D)
    return TODAY


# --- Task 1: setting rules ---------------------------------------------------------------------

def test_paid_by_stored_and_cleared(db, household_clean, today):
    h = household_clean
    pret = _pret(db, h)
    _set(h.user_a, pret, paid_by=h.user_a.id)
    assert pret.paid_by_user_id == h.user_a.id and pret.paid_by.id == h.user_a.id
    _set(h.user_a, pret, paid_by=0)
    assert pret.paid_by_user_id is None


def test_paid_by_refused_outside_rules(db, household_clean, today):
    h = household_clean
    dyn = _ccat(db, h.common, 'Courses', 'dynamic', pct=0.2)
    with pytest.raises(CategoryError):
        category_service.update_family(h.user_a, dyn, dyn.name, 'dynamic', True, None,
                                       {'common': {'percentage': 0.2, 'paid_by': h.user_a.id}})
    perso = _ccat(db, h.personal_a, 'Loyer A', 'fixed_amount', amount=500.0)
    with pytest.raises(CategoryError):
        category_service.update_family(h.user_a, perso, perso.name, 'fixed_amount', False, 'own',
                                       {'personal': {'amount': 500.0, 'paid_by': h.user_a.id}})
    pret = _pret(db, h)
    from app.services.household_service import create_household
    stranger = User(email='x@other.test', display_name='X')
    stranger.set_password('x')
    create_household('Autre', stranger)
    with pytest.raises(CategoryError):
        _set(h.user_a, pret, paid_by=stranger.id)
    assert pret.paid_by_user_id is None


def test_paid_by_refused_on_common_transfer(db, household_clean, today):
    h = household_clean
    cat = _ccat(db, h.common, 'Virement', 'fixed_amount', amount=0.0)
    cat.role = 'common_transfer'
    _db.session.commit()
    with pytest.raises(CategoryError):
        category_service._apply_settings(cat, {'amount': 0.0, 'paid_by': h.user_a.id})


def test_change_type_to_dynamic_clears_paid_by(db, household_clean, today):
    h = household_clean
    pret = _pret(db, h)
    _set(h.user_a, pret, paid_by=h.user_a.id)
    category_service.update_family(h.user_a, pret, pret.name, 'dynamic', True, None,
                                   {'common': {'percentage': 0.1}})
    assert pret.paid_by_user_id is None


def test_paid_by_locked_when_plan_validated(db, household_clean, today, mail_outbox):
    h = household_clean
    pret = _pret(db, h)
    charges = _ccat(db, h.common, 'Charges', 'fixed_amount', amount=2000.0)
    _set(h.user_a, pret, paid_by=h.user_a.id)
    plan = _ready(db, h)
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    assert cs.advance_locked(h.common, TODAY)
    with pytest.raises(CategoryError):
        _set(h.user_a, pret, paid_by=h.user_b.id)
    with pytest.raises(CategoryError):
        _set(h.user_a, pret, paid_by=h.user_a.id, amount=900.0)
    _db.session.refresh(pret)
    assert (pret.paid_by_user_id, pret.amount) == (h.user_a.id, 800.0)
    _set(h.user_a, charges, amount=2100.0)
    assert charges.amount == 2100.0
