"""Wipe every operation and what derives from it, keeping the configuration (one-off reset).

Kept: households, users, pots, categories (+ notices), bank accounts, bank connections and their
account links, settings, salary forecasts. The next sync of every link starts at `since`.
"""
from datetime import date, datetime, time, timedelta

from app.extensions import db
from app.models import (
    AlertLog, BankAccountLink, BudgetAdjustment, BudgetMonth, BudgetMonthCategory, ContributionCap,
    ContributionPlan, ContributionShare, ContributionValidation, EnvelopeCeiling, EnvelopePlan,
    Income, Operation, PendingBankMovement, PotAlertState, ProvisionAllocation, RefloatPlan,
)
from app.services.bank_sync_service import OVERLAP_DAYS

# children before parents (MariaDB enforces the foreign keys)
WIPED = (
    ContributionValidation, ContributionShare, ContributionCap, ProvisionAllocation, ContributionPlan,
    EnvelopeCeiling, EnvelopePlan, RefloatPlan, BudgetAdjustment, BudgetMonthCategory, BudgetMonth,
    Income, Operation, PendingBankMovement, AlertLog, PotAlertState,
)


def default_since(today: date) -> date:
    """The 26th of the month before `today`."""
    first = today.replace(day=1)
    return (first - timedelta(days=1)).replace(day=26)


def counts() -> dict:
    return {m.__tablename__: m.query.count() for m in WIPED}


def _delete(model):
    model.query.delete(synchronize_session=False)


def reset_operations(since: date) -> dict:
    """Delete everything in WIPED in one transaction; the next sync of every link starts at `since`.

    The sync window of a link is `last_synced_at − OVERLAP_DAYS`, so it is set to
    `since + OVERLAP_DAYS`: a failed first sync still restarts from `since`, never 90 days back.
    """
    done = counts()
    try:
        for model in WIPED:
            _delete(model)
        start = datetime.combine(since, time()) + timedelta(days=OVERLAP_DAYS)
        BankAccountLink.query.update({BankAccountLink.last_synced_at: start},
                                     synchronize_session=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return done
