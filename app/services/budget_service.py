from datetime import date
from app.extensions import db
from app.models.budget import BudgetMonth, BudgetMonthCategory
from app.models.category import Category
from app.models.pot import Pot
from app.models.user import User
from app.models.operation import Operation
from app.models.adjustment import BudgetAdjustment


def target_budget_month(income_date, income_start_day):
    """Return (year, month) of the budget month an income on income_date finances."""
    if income_start_day > 1 and income_date.day >= income_start_day:
        if income_date.month == 12:
            return income_date.year + 1, 1
        return income_date.year, income_date.month + 1
    return income_date.year, income_date.month


def budget_month_start(pot, year, month):
    """First day of budget month (year, month) of `pot` (see target_budget_month)."""
    start_day = pot.income_start_day or 1
    if start_day > 1:
        prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
        return date(prev_year, prev_month, start_day)
    return date(year, month, 1)


def current_budget_month(pot, today):
    """BudgetMonth of `pot` that `today` belongs to."""
    year, month = target_budget_month(today, pot.income_start_day or 1)
    return get_or_create_budget_month(pot.id, year, month)


def get_or_create_budget_month(pot_id, year, month):
    if pot_id is None:
        raise ValueError('pot_id is required to build a budget month')
    bm = BudgetMonth.query.filter_by(pot_id=pot_id, year=year, month=month).first()
    if not bm:
        bm = BudgetMonth(pot_id=pot_id, year=year, month=month, total_income=0.0)
        db.session.add(bm)
        db.session.commit()
    return bm


def dynamic_brut(category, disponible):
    """Budget of a dynamic category before carry-in: share of disponible clamped by min/max."""
    brut = disponible * (category.percentage or 0.0)
    if category.min_amount is not None:
        brut = max(category.min_amount, brut)
    if category.max_amount is not None:
        brut = min(category.max_amount, brut)
    return brut


def fixed_brut(category, budget_month):
    """Fixed amount of a category for a month; the common-transfer category carries the J6 share."""
    if category.role == 'common_transfer':
        from app.services.contribution_service import contribution_for
        pot = category.pot or db.session.get(Pot, category.pot_id)
        share = contribution_for(pot, budget_month.year, budget_month.month)
        if share is not None:
            return share
    return category.amount or 0.0


def pot_disponible(budget_month, categories=None):
    """Income left for dynamic categories: income − fixed amounts − refloat supplement (personal pot)."""
    pot = budget_month.pot or db.session.get(Pot, budget_month.pot_id)
    if pot.kind == 'common':
        from app.services.contribution_service import common_disponible, is_j6
        if is_j6(pot, budget_month.year, budget_month.month):
            return common_disponible(budget_month)
    if categories is None:
        categories = Category.query.filter_by(pot_id=budget_month.pot_id).all()
    fixed_total = sum(fixed_brut(c, budget_month) for c in categories
                      if c.type in ('fixed_auto', 'fixed_amount'))
    disponible = budget_month.total_income - fixed_total
    if pot.kind == 'personal':
        from app.services.refloat_service import supplement_requested_for
        disponible -= supplement_requested_for(pot, budget_month.year, budget_month.month)
    return max(0.0, disponible)


def recalculate_budget_month(budget_month):
    """Recalculate all category allocations for a BudgetMonth.

    Formula:
      disponible = total_income - sum(fixed amounts) - refloat supplement (personal pot)
      dynamic.base_allocated = ceiling (preset or stored slider, see ceiling_service) + carry_in
      fixed categories: base_allocated = amount + carry_in
    """
    from app.services import ceiling_service

    categories = Category.query.filter_by(pot_id=budget_month.pot_id).all()
    disponible = pot_disponible(budget_month, categories)

    bmcs = {}
    for cat in categories:
        bmc = BudgetMonthCategory.query.filter_by(
            budget_month_id=budget_month.id, category_id=cat.id
        ).first()
        if not bmc:
            carry_in = _compute_carry_in(budget_month, cat)
            bmc = BudgetMonthCategory(
                budget_month_id=budget_month.id,
                category_id=cat.id,
                carry_in=carry_in,
                base_allocated=0.0,
            )
            db.session.add(bmc)
        bmcs[cat.id] = bmc
    db.session.flush()

    # Note: carry_in is set at BudgetMonthCategory creation only.
    # Re-running recalculate_budget_month preserves the original carry_in
    # to avoid double-counting across multiple recalculations in the same month.
    pot = budget_month.pot or db.session.get(Pot, budget_month.pot_id)
    ceilings = ceiling_service.report(budget_month).envelopes
    allocations = {}
    if pot.kind == 'common':
        from app.services.contribution_service import get_plan as get_contribution_plan
        cplan = get_contribution_plan(pot, budget_month.year, budget_month.month)
        if cplan is not None:
            allocations = {a.category_id: a.amount for a in cplan.allocations}

    for cat in categories:
        if cat.type in ('fixed_auto', 'fixed_amount'):
            budget_brut = fixed_brut(cat, budget_month)
        else:  # dynamic
            budget_brut = ceilings[cat.id].amount
        budget_brut += allocations.get(cat.id, 0.0)
        bmcs[cat.id].base_allocated = budget_brut + bmcs[cat.id].carry_in

    db.session.commit()


def _compute_carry_in(budget_month, category):
    """Compute carry-in from previous month for a category."""
    if not category.carry_over:
        return 0.0
    prev_month = budget_month.month - 1
    prev_year = budget_month.year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1
    prev_bm = BudgetMonth.query.filter_by(
        pot_id=budget_month.pot_id, year=prev_year, month=prev_month
    ).first()
    if not prev_bm:
        return 0.0
    prev_bmc = BudgetMonthCategory.query.filter_by(
        budget_month_id=prev_bm.id, category_id=category.id
    ).first()
    if not prev_bmc or prev_bmc.closed_at is not None:
        return 0.0
    prev_spent = get_spent_for_category(prev_bm, category)
    prev_effective = get_category_effective_budget(prev_bm, category)
    surplus = prev_effective - prev_spent
    return max(0.0, surplus)


def get_category_effective_budget(budget_month, category):
    """Effective budget = base_allocated + sum of adjustments."""
    bmc = BudgetMonthCategory.query.filter_by(
        budget_month_id=budget_month.id, category_id=category.id
    ).first()
    if not bmc:
        return 0.0
    adjustments_in = db.session.query(
        db.func.sum(BudgetAdjustment.amount)
    ).filter_by(budget_month_id=budget_month.id, to_category_id=category.id).scalar() or 0.0
    adjustments_out = db.session.query(
        db.func.sum(BudgetAdjustment.amount)
    ).filter_by(budget_month_id=budget_month.id, from_category_id=category.id).scalar() or 0.0
    return bmc.base_allocated + adjustments_in - adjustments_out


def _month_bounds(year, month):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def budget_month_bounds(pot, year, month):
    """[start, end) dates of budget month (year, month) of `pot`."""
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return budget_month_start(pot, year, month), budget_month_start(pot, next_year, next_month)


def _spent_query(category, year, month, *columns):
    """Operations counted in the spent of `category` during budget month (year, month)."""
    pot = category.pot or db.session.get(Pot, category.pot_id)
    start, end = budget_month_bounds(pot, year, month)
    return db.session.query(*columns).filter(
        Operation.category_id == category.id,
        db.or_(
            db.and_(Operation.direction == 'debit', Operation.classification == 'category'),
            db.and_(Operation.direction == 'credit', Operation.classification == 'refund'),
        ),
        Operation.date >= start,
        Operation.date < end,
    )


def spent_for_category(category, year, month):
    """Debits minus refunds (credits) classified on this category during budget month (year, month)."""
    signed = db.case((Operation.direction == 'credit', -Operation.amount), else_=Operation.amount)
    result = _spent_query(category, year, month, db.func.sum(signed)).scalar()
    return float(result or 0.0)


def spent_operations(category, year, month):
    """The operations summed by `spent_for_category`, most recent first."""
    return (_spent_query(category, year, month, Operation)
            .order_by(Operation.date.desc(), Operation.id.desc()).all())


def pot_spent_total(pot_id, year, month):
    """Total spent over all categories of a pot (internal/off_budget/unclassified excluded)."""
    cats = Category.query.filter_by(pot_id=pot_id).all()
    return sum(spent_for_category(c, year, month) for c in cats)


def get_spent_for_category(budget_month, category):
    """Spent for this category in this budget month."""
    return spent_for_category(category, budget_month.year, budget_month.month)


def compute_daily_metrics(budget_month, category, current_day, days_in_month, spent):
    """Return dict with ideal_daily, ideal_cumulated, remaining_daily."""
    effective = get_category_effective_budget(budget_month, category)
    ideal_daily = effective / days_in_month if days_in_month > 0 else 0.0
    ideal_cumulated = effective * current_day / days_in_month if days_in_month > 0 else 0.0
    days_remaining = max(1, days_in_month - current_day)
    remaining_daily = max(0.0, (effective - spent) / days_remaining)
    return {
        'effective': effective,
        'ideal_daily': ideal_daily,
        'ideal_cumulated': ideal_cumulated,
        'remaining_daily': remaining_daily,
        'spent': spent,
        'balance': effective - spent,
    }
