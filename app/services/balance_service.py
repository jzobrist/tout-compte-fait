"""Real balance and "argent vraiment libre" of a pot (J4)."""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.extensions import db
from app.models.bank_account import BankAccount
from app.models.budget import BudgetMonthCategory
from app.models.category import Category
from app.models.operation import Operation
from app.models.user import User
from app.services.budget_service import (
    budget_month_start, get_category_effective_budget, get_spent_for_category, target_budget_month,
)


class EnvelopeError(Exception):
    pass


@dataclass
class PotBalance:
    known: bool
    value: float | None
    booked: float | None
    pending: float | None
    as_of: datetime | None
    missing: list = field(default_factory=list)


@dataclass
class FreeMoney:
    balance: PotBalance
    income_received: bool
    remaining: float
    pending_entries: float
    transfer_expected: float
    value: float | None


def pot_balance(pot):
    """Sum of the balances of the pot's non-archived accounts; unknown if one was never synced."""
    accounts = (BankAccount.query.filter_by(pot_id=pot.id, archived=False)
                .order_by(BankAccount.id).all())
    missing = [a.name for a in accounts if a.balance_value is None]
    if missing or not accounts:
        return PotBalance(False, None, None, None, None, missing)
    value = sum(a.balance_value for a in accounts)
    booked = sum(a.balance_booked if a.balance_booked is not None else a.balance_value
                 for a in accounts)
    as_of = min((a.balance_at for a in accounts if a.balance_at), default=None)
    return PotBalance(True, value, booked, value - booked, as_of, [])


def _bmc(bm, category):
    return BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=category.id).first()


def category_remaining(bm, category):
    """What is still planned to be spent on this envelope this month (0 if closed or overspent)."""
    from app.services import contribution_service as cs
    if category.role == 'common_transfer':
        due = cs.due_for(bm.pot, bm.year, bm.month)
        if due is not None:   # + charges paid for the common pot from this account, not yet debited
            common = cs._common_of(bm.pot)
            owner = db.session.get(User, bm.pot.owner_id)
            return due + cs.advance_pending(common, owner, bm.year, bm.month)
    if category.paid_by_user_id is not None and cs.get_plan(bm.pot, bm.year, bm.month) is not None:
        return 0.0   # paid from a member's personal account: never leaves the common accounts
    bmc = _bmc(bm, category)
    if bmc is not None and bmc.closed_at is not None:
        return 0.0
    return max(0.0, get_category_effective_budget(bm, category) - get_spent_for_category(bm, category))


def pending_entries_total(pot, since=None):
    """Manual entries of the pot still awaiting their bank debit (already spent, not yet in balance).

    `since` bounds them (free money: from the previous budget month) so stale entries never linked
    to their debit do not lower the balance forever.
    """
    query = db.session.query(db.func.sum(Operation.amount))
    if since is not None:
        query = query.filter(Operation.date >= since)
    total = query.filter(
        Operation.pot_id == pot.id,
        Operation.bank_reference.is_(None),
        Operation.classification == 'category',
        Operation.direction == 'debit',
    ).scalar()
    return float(total or 0.0)


def free_money(pot, bm):
    """Argent vraiment libre = balance − pending entries − remaining of every envelope + J6 transfers
    still expected (common pot). A personal pot reserves its due transfer through the remaining of
    its « Virement vers le commun » category."""
    balance = pot_balance(pot)
    remaining = sum(category_remaining(bm, c) for c in Category.query.filter_by(pot_id=pot.id))
    prev = (bm.year - 1, 12) if bm.month == 1 else (bm.year, bm.month - 1)
    pending = pending_entries_total(pot, since=budget_month_start(pot, *prev))
    transfer_expected = 0.0
    from app.services import contribution_service
    income_received = bm.total_income > 0
    j6_plan = contribution_service.get_plan(pot, bm.year, bm.month) if pot.kind == 'common' else None
    if j6_plan is not None:
        transfer_expected = sum(m.due for m in contribution_service.member_status(j6_plan))
        income_received = True
    value = None
    if balance.known:
        value = balance.value - pending - remaining + transfer_expected
    return FreeMoney(balance, income_received, remaining, pending,
                     transfer_expected, value)


def _check_current(bm, today):
    pot = bm.pot
    if (bm.year, bm.month) != target_budget_month(today, pot.income_start_day or 1):
        raise EnvelopeError('Seul le mois en cours peut être modifié.')


def _set_closed(bm, category, today, closed):
    _check_current(bm, today)
    if category.pot_id != bm.pot_id:
        raise EnvelopeError('Enveloppe d\'un autre pot.')
    bmc = _bmc(bm, category)
    if bmc is None:
        bmc = BudgetMonthCategory(budget_month_id=bm.id, category_id=category.id,
                                  base_allocated=0.0, carry_in=0.0)
        db.session.add(bmc)
    if closed:
        # before the close changes the capacities: a closed envelope keeps its current ceiling
        from app.services import ceiling_service
        ceiling_service.pin_current(bm.pot, bm.year, bm.month, category,
                                    commit=False)
    bmc.closed_at = datetime.now(timezone.utc).replace(tzinfo=None) if closed else None
    db.session.commit()


def close_envelope(bm, category, today):
    _set_closed(bm, category, today, True)


def reopen_envelope(bm, category, today):
    _set_closed(bm, category, today, False)
