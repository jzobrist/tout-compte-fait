from app.extensions import db
from app.models.budget import BudgetMonth
from app.models.income import Income
from app.models.refloat import RefloatPlan
from app.services.budget_service import (
    _compute_carry_in, get_or_create_budget_month, recalculate_budget_month, target_budget_month,
)


def income_budget_month(pot, on):
    """(year, month) of the budget month an income dated `on` finances in `pot`."""
    return target_budget_month(on, pot.income_start_day or 1)


def _budget_month(pot, on, commit):
    year, month = income_budget_month(pot, on)
    if commit:
        return get_or_create_budget_month(pot.id, year, month)
    bm = BudgetMonth.query.filter_by(pot_id=pot.id, year=year, month=month).first()
    if not bm:
        bm = BudgetMonth(pot_id=pot.id, year=year, month=month, total_income=0.0)
        db.session.add(bm)
        db.session.flush()
    return bm


def record_income(pot, user, amount, label, on, operation=None, commit=True):
    """Record an income in `pot` and add it to the budget month it finances.

    With commit=False nothing is committed or recalculated: the caller owns the transaction.
    """
    income = Income(
        pot_id=pot.id, user_id=user.id, amount=amount, label=label, date=on,
        operation_id=operation.id if operation is not None else None,
    )
    db.session.add(income)
    bm = _budget_month(pot, on, commit)
    bm.total_income += amount
    if commit:
        db.session.commit()
        recalculate_budget_month(bm)
    return income


def remove_income(income, commit=True):
    """Delete an income and subtract it from the budget month it financed."""
    bm = _budget_month(income.pot, income.date, commit)
    bm.total_income = max(0.0, bm.total_income - income.amount)
    db.session.delete(income)
    if commit:
        db.session.commit()
        recalculate_budget_month(bm)


def reassign_incomes(pot):
    """Recompute every budget month of `pot` from its incomes and current income_start_day (used
    after the start day changed): total_income, then carry-ins in chronological order. Remise à
    flot plans from the first changed month on are deleted so their deficit is measured again."""
    totals = {}
    for income in Income.query.filter_by(pot_id=pot.id).all():
        key = income_budget_month(pot, income.date)
        totals[key] = totals.get(key, 0.0) + income.amount
    changed = set()
    for bm in BudgetMonth.query.filter_by(pot_id=pot.id).all():
        new_total = totals.pop((bm.year, bm.month), 0.0)
        if abs(bm.total_income - new_total) > 1e-9:
            bm.total_income = new_total
            changed.add((bm.year, bm.month))
    for (year, month), amount in totals.items():
        db.session.add(BudgetMonth(pot_id=pot.id, year=year, month=month, total_income=amount))
        changed.add((year, month))
    db.session.commit()

    # carry_in is normally frozen at creation; here the months it came from changed, so
    # recompute it once, oldest month first (each month's carry reads the previous one).
    months = BudgetMonth.query.filter_by(pot_id=pot.id).order_by(BudgetMonth.year, BudgetMonth.month).all()
    for bm in months:
        for bmc in bm.category_budgets:
            carry = _compute_carry_in(bm, bmc.category)
            if abs(bmc.carry_in - carry) > 1e-9:
                bmc.carry_in = carry
                changed.add((bm.year, bm.month))
        recalculate_budget_month(bm)

    if changed:
        first = min(changed)
        stale = [p for p in RefloatPlan.query.filter_by(pot_id=pot.id).all() if (p.year, p.month) >= first]
        for plan in stale:
            db.session.delete(plan)
        db.session.commit()
        for bm in months:
            if (bm.year, bm.month) >= first:
                recalculate_budget_month(bm)
