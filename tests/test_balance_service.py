from datetime import date, datetime

import pytest

from app.models import BankAccount, BudgetMonthCategory, Category, Operation
from app.services.balance_service import (
    EnvelopeError, category_remaining, close_envelope, free_money, pending_entries_total,
    pot_balance, reopen_envelope,
)
from app.services.budget_service import (
    budget_month_start, get_or_create_budget_month, recalculate_budget_month,
)

TODAY = date(2026, 11, 15)



def _account(db, pot, name='Compte', booked=None, value=None, archived=False):
    acc = BankAccount(pot_id=pot.id, name=name, bank_name='Banque', balance_booked=booked,
                      balance_value=value, archived=archived,
                      balance_at=datetime(2026, 11, 15, 6, 0) if value is not None else None)
    db.session.add(acc)
    db.session.commit()
    return acc


def _cat(db, pot, name, amount=100.0, type_='fixed_amount', carry_over=False, **kw):
    c = Category(name=name, type=type_, amount=amount, pot_id=pot.id, carry_over=carry_over, **kw)
    db.session.add(c)
    db.session.commit()
    return c


def _bm(pot, year=2026, month=11, income=3000.0):
    bm = get_or_create_budget_month(pot.id, year, month)
    bm.total_income = income
    recalculate_budget_month(bm)
    return bm


def _op(db, h, pot, cat, amount, direction='debit', classification='category', bank_ref='R',
        on=date(2026, 11, 10)):
    op = Operation(pot_id=pot.id, user_id=h.user_a.id, category_id=cat.id, amount=amount,
                   date=on, direction=direction, classification=classification,
                   bank_reference=bank_ref)
    db.session.add(op)
    db.session.commit()
    return op


def test_pot_balance_sums_non_archived_accounts(db, household_clean):
    pot = household_clean.common
    _account(db, pot, 'A', booked=900.00, value=250.00)
    _account(db, pot, 'B', booked=100.00, value=800.00)
    _account(db, pot, 'Vieux', booked=1000.0, value=1000.0, archived=True)
    bal = pot_balance(pot)
    assert bal.known is True
    assert bal.value == pytest.approx(1050.00)
    assert bal.booked == pytest.approx(1000.00)
    assert bal.pending == pytest.approx(50.00)
    assert bal.missing == []


def test_pot_balance_unknown_when_account_never_synced(db, household_clean):
    pot = household_clean.common
    _account(db, pot, 'A', booked=10.0, value=10.0)
    _account(db, pot, 'Compte CSV')
    bal = pot_balance(pot)
    assert bal.known is False
    assert bal.value is None
    assert bal.missing == ['Compte CSV']


def test_remaining_overspent_is_zero(db, household_clean):
    h = household_clean
    over = _cat(db, h.common, 'Over')
    refunded = _cat(db, h.common, 'Refunded')
    bm = _bm(h.common)
    _op(db, h, h.common, over, 130.0, bank_ref='R1')
    _op(db, h, h.common, refunded, 50.0, direction='credit', classification='refund', bank_ref='R2')
    assert category_remaining(bm, over) == 0.0
    assert category_remaining(bm, refunded) == pytest.approx(150.0)


def test_remaining_closed_is_zero(db, household_clean):
    h = household_clean
    cat = _cat(db, h.common, 'Impôts', amount=300.0)
    bm = _bm(h.common)
    _op(db, h, h.common, cat, 240.0)
    assert category_remaining(bm, cat) == pytest.approx(60.0)
    close_envelope(bm, cat, TODAY)
    assert category_remaining(bm, cat) == 0.0


def test_free_money_formula(db, household_clean):
    h = household_clean
    _account(db, h.common, 'A', booked=1000.0, value=1000.0)
    a = _cat(db, h.common, 'A', amount=300.0)
    _cat(db, h.common, 'B', amount=200.0)
    bm = _bm(h.common)
    _op(db, h, h.common, a, 40.0, bank_ref=None)  # manual entry awaiting its debit
    assert pending_entries_total(h.common) == pytest.approx(40.0)
    fm = free_money(h.common, bm)
    assert fm.remaining == pytest.approx(460.0)
    assert fm.pending_entries == pytest.approx(40.0)
    assert fm.income_received is True
    assert fm.value == pytest.approx(1000.0 - 40.0 - 460.0)


def test_free_money_none_when_balance_unknown(db, household_clean):
    h = household_clean
    _account(db, h.common, 'CSV')
    bm = _bm(h.common)
    fm = free_money(h.common, bm)
    assert fm.value is None
    assert fm.balance.known is False


def test_close_then_reopen_current_month(db, household_clean):
    h = household_clean
    cat = _cat(db, h.common, 'Impôts')
    bm = _bm(h.common)
    close_envelope(bm, cat, TODAY)
    bmc = BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=cat.id).one()
    assert bmc.closed_at is not None
    reopen_envelope(bm, cat, TODAY)
    assert bmc.closed_at is None


def test_close_past_month_raises(db, household_clean):
    h = household_clean
    cat = _cat(db, h.common, 'Impôts')
    bm = _bm(h.common, month=10)
    with pytest.raises(EnvelopeError):
        close_envelope(bm, cat, TODAY)


def test_closed_envelope_not_carried_over(db, household_clean):
    h = household_clean
    closed = _cat(db, h.common, 'Closed', amount=100.0, carry_over=True)
    open_ = _cat(db, h.common, 'Open', amount=100.0, carry_over=True)
    oct_bm = _bm(h.common, month=10)
    _op(db, h, h.common, closed, 40.0, bank_ref='R1', on=date(2026, 10, 5))
    _op(db, h, h.common, open_, 40.0, bank_ref='R2', on=date(2026, 10, 5))
    close_envelope(oct_bm, closed, date(2026, 10, 20))
    nov_bm = _bm(h.common, month=11)
    carry = {b.category_id: b.carry_in for b in nov_bm.category_budgets}
    assert carry[closed.id] == 0.0
    assert carry[open_.id] == pytest.approx(60.0)


def test_budget_month_start(db, household_clean):
    pot = household_clean.common
    pot.income_start_day = 25
    db.session.commit()
    assert budget_month_start(pot, 2026, 12) == date(2026, 11, 25)
    assert budget_month_start(pot, 2026, 1) == date(2025, 12, 25)
    pot.income_start_day = 1
    db.session.commit()
    assert budget_month_start(pot, 2026, 12) == date(2026, 12, 1)
