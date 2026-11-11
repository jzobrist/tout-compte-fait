from dataclasses import dataclass
from datetime import timedelta

from app.extensions import db
from app.models.operation import Operation
from app.services.budget_service import get_or_create_budget_month, recalculate_budget_month

WINDOW_BEFORE = timedelta(days=10)
WINDOW_AFTER = timedelta(days=3)


@dataclass
class ReconcileResult:
    auto_linked: int
    conflicts: int


def eligible_entries(movement, any_amount):
    """Pending debit entries a bank movement may be linked to.

    Entries of the account's pot, plus manual entries of the household's common pot made by
    the owner when the account belongs to a personal pot. With `any_amount=False`, only
    entries of the same amount (rounded to cents).
    """
    if movement.direction != 'debit' or movement.bank_account is None:
        return []
    pot = movement.bank_account.pot
    clauses = [Operation.pot_id == pot.id]
    if pot.kind == 'personal':
        from app.models.pot import Pot
        common = Pot.query.filter_by(household_id=pot.household_id, kind='common').first()
        if common is not None:
            clauses.append(db.and_(
                Operation.pot_id == common.id,
                Operation.user_id == pot.owner_id,
                Operation.auto_generated.isnot(True),
            ))
    entries = (Operation.query
               .filter(Operation.bank_reference.is_(None),
                       Operation.direction == 'debit',
                       Operation.classification == 'category',
                       db.or_(*clauses))
               .order_by(Operation.date, Operation.id).all())
    if not any_amount:
        entries = [e for e in entries if round(e.amount, 2) == round(movement.amount, 2)]
    return entries


def candidates_for(movement):
    day = movement.bank_date
    return [e for e in eligible_entries(movement, any_amount=False)
            if day - WINDOW_BEFORE <= e.date <= day + WINDOW_AFTER]


def _recalculate(entry):
    recalculate_budget_month(get_or_create_budget_month(entry.pot_id, entry.date.year, entry.date.month))


def link(entry, movement):
    """Merge the bank movement into the pending entry (single transaction)."""
    account_id, reference = movement.bank_account_id, movement.bank_reference
    label, bank_date, amount = movement.bank_label, movement.bank_date, movement.amount
    db.session.delete(movement)
    db.session.flush()  # free the (account, reference) unique slot
    entry.bank_account_id = account_id
    entry.bank_reference = reference
    entry.bank_label = label
    entry.bank_date = bank_date
    entry.amount = amount
    db.session.commit()
    _recalculate(entry)
    return entry


def unlink(entry):
    """Split a reconciled entry back into a pending entry and a to-classify movement."""
    if entry.bank_reference is None or entry.entered_amount is None:
        raise ValueError("Cette opération n'est pas un rapprochement.")
    account = entry.bank_account
    movement = Operation(
        pot_id=account.pot_id, bank_account_id=account.id, user_id=entry.user_id,
        amount=entry.amount, date=entry.bank_date, label=entry.bank_label,
        direction='debit', classification=None, bank_reference=entry.bank_reference,
        bank_label=entry.bank_label, bank_date=entry.bank_date,
    )
    entry.bank_account_id = None
    entry.bank_reference = None
    entry.bank_label = None
    entry.bank_date = None
    entry.amount = entry.entered_amount
    db.session.flush()  # free the unique slot before the movement takes the reference
    db.session.add(movement)
    db.session.commit()
    _recalculate(entry)
    return movement


def reconcile_movements(movements):
    """Auto-link debits having exactly one candidate; count ambiguous ones as conflicts."""
    linked = conflicts = 0
    for movement in sorted((m for m in movements if m.direction == 'debit'),
                           key=lambda m: (m.bank_date, m.id)):
        candidates = candidates_for(movement)
        if len(candidates) == 1:
            link(candidates[0], movement)
            linked += 1
        elif len(candidates) > 1:
            conflicts += 1
    return ReconcileResult(linked, conflicts)
