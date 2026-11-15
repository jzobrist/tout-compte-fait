"""flask reset-operations: wipe operations and derived data, keep the configuration."""
from datetime import date, datetime

import pytest

from app.extensions import db as _db
from app.models import (
    AlertLog, BankAccount, BankAccountLink, BudgetAdjustment, BudgetMonth, BudgetMonthCategory,
    Category, ContributionCap, ContributionPlan, ContributionShare, ContributionValidation,
    EnvelopeCeiling, EnvelopePlan, Income, Operation, PendingBankMovement, PotAlertState,
    ProvisionAllocation, RefloatPlan, SalaryForecast,
)
from app.services import bank_sync_service, reset_service
from app.services.pointage_service import classify
from tests.test_bank_sync import FakeSyncClient, make_conn, make_link
from tests.test_contribution_service import TODAY, _ccat, _credit, _j6

WIPED = (AlertLog, BudgetAdjustment, BudgetMonth, BudgetMonthCategory, ContributionCap,
         ContributionPlan, ContributionShare, ContributionValidation, EnvelopeCeiling, EnvelopePlan,
         Income, Operation, PendingBankMovement, PotAlertState, ProvisionAllocation, RefloatPlan)


def _populate(db, h):
    """A J6 month with a validated plan, a contribution, a salary, a pending movement, alerts."""
    from app.services import contribution_service as cs
    _ccat(db, h.common, 'Courses', pct=0.3)
    plan = _j6(db, h)
    cs.validate(plan, h.user_a, TODAY)
    cs.validate(plan, h.user_b, TODAY)
    classify(_credit(db, h, 500.0), h.user_a, 'contribution', contributor=h.user_b)
    acc = BankAccount.query.filter_by(pot_id=h.common.id).first()
    salary = Operation(pot_id=h.personal_a.id, user_id=h.user_a.id, amount=3000.0,
                       date=date(2026, 10, 28), bank_date=date(2026, 10, 28), direction='credit',
                       bank_reference='SAL', bank_label='SALAIRE')
    db.session.add(salary)
    db.session.commit()
    classify(salary, h.user_a, 'income')
    acc.overdraft_limit = 300.0
    db.session.add(PendingBankMovement(bank_account_id=acc.id, amount=12.0, label='CB',
                                       direction='debit', date=date(2026, 11, 15)))
    db.session.add(PotAlertState(pot_id=h.common.id, alert_type='free_negative', active=True))
    db.session.add(AlertLog(pot_id=h.common.id, alert_type='pace'))
    db.session.add(SalaryForecast(user_id=h.user_a.id, member_id=h.user_b.id, amount=2100.0))
    h.common.income_start_day = 26
    h.user_a.household.solidarity_margin = 50.0
    conn = make_conn(h.user_a, 'app-a')
    link = make_link(conn, acc, 'uid-1')
    link.last_synced_at = datetime(2026, 11, 15, 6, 0)
    db.session.commit()
    return acc, conn, link


def test_default_since():
    assert reset_service.default_since(date(2026, 11, 15)) == date(2026, 10, 26)
    assert reset_service.default_since(date(2027, 1, 3)) == date(2026, 12, 26)


def test_reset_deletes_operations_and_derived_keeps_config(db, household_clean):
    h = household_clean
    acc, conn, link = _populate(db, h)
    empty = [m.__tablename__ for m in (Operation, Income, BudgetMonth, ContributionPlan, ContributionShare,
                                       EnvelopePlan, PendingBankMovement) if m.query.count() == 0]
    assert empty == []
    categories = Category.query.count()
    counts = reset_service.counts()
    assert counts['operation'] == Operation.query.count()
    done = reset_service.reset_operations(date(2026, 10, 26))
    assert done == counts
    for model in WIPED:
        assert model.query.count() == 0, model.__tablename__
    _db.session.expire_all()
    assert Category.query.count() == categories
    assert _db.session.get(BankAccount, acc.id).overdraft_limit == 300.0
    assert SalaryForecast.query.count() == 1
    assert h.common.income_start_day == 26 and h.user_a.household.solidarity_margin == 50.0
    assert BankAccountLink.query.count() == 1 and conn.status == 'active'


def test_next_sync_starts_at_since(db, household_clean, monkeypatch):
    h = household_clean
    acc, conn, link = _populate(db, h)
    reset_service.reset_operations(date(2026, 10, 26))
    fake = FakeSyncClient()
    fake.bals['uid-1'] = []
    monkeypatch.setattr(bank_sync_service, 'get_client', lambda c: fake)
    bank_sync_service.sync_connection(conn, today=TODAY)
    assert fake.calls == [('uid-1', date(2026, 10, 26))]


def test_reset_is_atomic(db, household_clean, monkeypatch):
    h = household_clean
    _populate(db, h)
    before = Operation.query.count()
    real = reset_service._delete

    def boom(model):
        if model is Operation:
            raise RuntimeError('boom')
        real(model)
    monkeypatch.setattr(reset_service, '_delete', boom)
    with pytest.raises(RuntimeError):
        reset_service.reset_operations(date(2026, 10, 26))
    assert Operation.query.count() == before
    assert ContributionPlan.query.count() == 1


def test_cli_reset_requires_confirmation(app, db, household_clean):
    h = household_clean
    _populate(db, h)
    runner = app.test_cli_runner()
    res = runner.invoke(args=['reset-operations', '--since', '2026-10-26', '--no-sync'], input='n\n')
    assert Operation.query.count() > 0
    assert 'operation' in res.output
    res = runner.invoke(args=['reset-operations', '--since', '2026-10-26', '--no-sync', '--yes'])
    assert res.exit_code == 0, res.output
    assert Operation.query.count() == 0
