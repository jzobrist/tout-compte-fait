"""Plafonds figés (« Plan du mois »): ceiling of each dynamic envelope, preset and stored sliders.

Without a stored row an envelope follows its preset (percentage of the disponible minus the
refloat auto cut); a stored row is a slider the member moved. Once the month is validated
(`EnvelopePlan.validated_at`) everything is frozen.
"""
import math
from dataclasses import dataclass, field

from app.extensions import db
from app.models.budget import BudgetMonthCategory
from app.models.category import Category
from app.models.ceiling import EnvelopeCeiling, EnvelopePlan
from app.services import refloat_service
from app.services.budget_service import (
    dynamic_brut, get_or_create_budget_month, get_spent_for_category, pot_disponible,
    recalculate_budget_month,
)
from app.services.refloat_service import CutReport, envelope_cuts, monthly_share


class CeilingError(Exception):
    """str(e) in {'frozen', 'locked', 'waiting', 'not_frozen'}."""


@dataclass
class Envelope:
    amount: float
    auto: bool
    low: float
    high: float
    locked: bool


@dataclass
class CeilingReport:
    envelopes: dict = field(default_factory=dict)
    frozen: bool = False
    unabsorbed: float = 0.0
    unallocated: float = None


def get_plan(pot, year, month):
    return EnvelopePlan.query.filter_by(pot_id=pot.id, year=year, month=month).first()


def is_frozen(pot, year, month):
    plan = get_plan(pot, year, month)
    return plan is not None and plan.validated_at is not None


def report(bm):
    """Ceilings of every dynamic envelope of the budget month. Never writes."""
    from app.models.pot import Pot
    pot = bm.pot or db.session.get(Pot, bm.pot_id)
    cats = Category.query.filter_by(pot_id=pot.id, type='dynamic').order_by(Category.id).all()
    eplan = get_plan(pot, bm.year, bm.month)
    stored = {c.category_id: c.amount for c in eplan.ceilings} if eplan else {}
    frozen = eplan is not None and eplan.validated_at is not None
    rplan = refloat_service.get_plan(pot, bm.year, bm.month)
    has_deficit = rplan is not None and rplan.deficit > 0
    cuts = envelope_cuts(rplan, bm) if has_deficit else CutReport()
    disponible = pot_disponible(bm)
    result = CeilingReport(frozen=frozen, unabsorbed=cuts.unabsorbed)
    for cat in cats:
        brut = dynamic_brut(cat, disponible)
        bmc = BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=cat.id).first()
        closed = bmc is not None and bmc.closed_at is not None
        amount = stored[cat.id] if cat.id in stored else brut - cuts.cuts.get(cat.id, 0.0)
        low = max(cat.min_amount or 0.0, get_spent_for_category(bm, cat))
        # No max_amount: a personal slider is only bounded by what is left to allocate (below).
        ceiling = cat.max_amount if cat.max_amount is not None else (
            math.inf if pot.kind == 'personal' else brut)
        high = max(ceiling, amount)
        result.envelopes[cat.id] = Envelope(amount=amount, auto=cat.id not in stored,
                                            low=low, high=high, locked=frozen or closed)
    # Task 3: common extra pass goes here (single place, before the personal unallocated bound).
    if pot.kind == 'personal':
        share = monthly_share(rplan) if has_deficit else 0.0
        result.unallocated = disponible - share - sum(e.amount for e in result.envelopes.values())
        room = max(0.0, result.unallocated)
        for env in result.envelopes.values():
            env.high = (max(env.amount, min(env.high, env.amount + room)) if not env.locked
                        else min(env.high, env.amount))
    return result


def dynamic_total(bm):
    return sum(e.amount for e in report(bm).envelopes.values())


def _recalculate(pot, year, month):
    if pot.kind == 'common':
        from app.services import contribution_service as cs
        if cs.is_j6(pot, year, month):
            cs.recalculate_household(pot, year, month)
            return
    recalculate_budget_month(get_or_create_budget_month(pot.id, year, month))


def set_ceiling(pot, year, month, category, amount):
    if category.pot_id != pot.id or category.type != 'dynamic':
        raise ValueError('not a dynamic category of this pot')
    if is_frozen(pot, year, month):
        raise CeilingError('frozen')
    bm = get_or_create_budget_month(pot.id, year, month)
    recalculate_budget_month(bm)
    env = report(bm).envelopes[category.id]
    if env.locked:
        raise CeilingError('locked')
    amount = min(env.high, max(env.low, float(amount)))
    plan = get_plan(pot, year, month)
    if plan is None:
        plan = EnvelopePlan(pot_id=pot.id, year=year, month=month)
        db.session.add(plan)
        db.session.flush()
    row = next((c for c in plan.ceilings if c.category_id == category.id), None)
    if row is None:
        db.session.add(EnvelopeCeiling(plan_id=plan.id, category_id=category.id, amount=amount))
    else:
        row.amount = amount
    db.session.commit()
    _recalculate(pot, year, month)


def reset(pot, year, month):
    """Forget the stored sliders (back to the preset). Refused once the month is frozen."""
    plan = get_plan(pot, year, month)
    if plan is None:
        return
    if plan.validated_at is not None:
        raise CeilingError('frozen')
    EnvelopeCeiling.query.filter_by(plan_id=plan.id).delete(synchronize_session=False)
    db.session.expire(plan)
    db.session.commit()
