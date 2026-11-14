"""Plafonds figés (« Plan du mois »): ceiling of each dynamic envelope, preset and stored sliders.

Without a stored row an envelope follows its preset (percentage of the disponible minus the
refloat auto cut); a stored row is a slider the member moved. Once the month is validated
(`EnvelopePlan.validated_at`) everything is frozen.
"""
import math
from datetime import datetime, timezone
from dataclasses import dataclass, field

from app.extensions import db
from app.models.budget import BudgetMonth, BudgetMonthCategory
from app.models.category import Category
from app.models.ceiling import EnvelopeCeiling, EnvelopePlan
from app.services import refloat_service
from app.services.budget_service import (
    dynamic_brut, get_or_create_budget_month, get_spent_for_category, pot_disponible,
    recalculate_budget_month,
)
from app.services.refloat_service import CutReport, envelope_cuts, monthly_share

EPS = 0.005


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
    extra_unabsorbed: float = 0.0   # part of `unabsorbed` left by the solidarity extra cut only
    unallocated: float = None


def get_plan(pot, year, month):
    return EnvelopePlan.query.filter_by(pot_id=pot.id, year=year, month=month).first()


def _legacy_validated(pot, year, month):
    """Common month whose ContributionPlan is validated: its shares are frozen, so are its ceilings
    (also for plans validated before this feature, which have no EnvelopePlan)."""
    if pot.kind != 'common':
        return False
    from app.services import contribution_service as cs
    cplan = cs.get_plan(pot, year, month)
    return cplan is not None and cplan.status == 'validated'


def is_frozen(pot, year, month):
    plan = get_plan(pot, year, month)
    return (plan is not None and plan.validated_at is not None) or _legacy_validated(pot, year, month)


def freeze_legacy(pot, year, month):
    """Store the ceilings of a common month validated before this feature (lazy, idempotent)."""
    if not _legacy_validated(pot, year, month):
        return
    plan = get_plan(pot, year, month)
    if plan is None or plan.validated_at is None:
        freeze(pot, year, month)


def pin_current(pot, year, month, category, commit=True):
    """Store the envelope's current amount as its ceiling (closing: « plafond = valeur actuelle »).
    No-op when the month is frozen or the envelope already has a stored ceiling."""
    if category.type != 'dynamic' or is_frozen(pot, year, month):
        return
    bm = get_or_create_budget_month(pot.id, year, month)
    env = report(bm).envelopes.get(category.id)
    if env is None or not env.auto:
        return
    plan = _upsert_plan(pot, year, month)
    db.session.add(EnvelopeCeiling(plan_id=plan.id, category_id=category.id, amount=env.amount))
    if commit:
        db.session.commit()


def report(bm):
    """Ceilings of every dynamic envelope of the budget month. Never writes."""
    from app.models.pot import Pot
    pot = bm.pot or db.session.get(Pot, bm.pot_id)
    cats = Category.query.filter_by(pot_id=pot.id, type='dynamic').order_by(Category.id).all()
    eplan = get_plan(pot, bm.year, bm.month)
    stored = {c.category_id: c.amount for c in eplan.ceilings} if eplan else {}
    frozen = is_frozen(pot, bm.year, bm.month)
    rplan = refloat_service.get_plan(pot, bm.year, bm.month)
    has_deficit = rplan is not None and rplan.deficit > 0
    cuts = envelope_cuts(rplan, bm) if has_deficit else CutReport()
    disponible = pot_disponible(bm)
    result = CeilingReport(frozen=frozen, unabsorbed=cuts.unabsorbed)
    for cat in cats:
        brut = dynamic_brut(cat, disponible)
        bmc = BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=cat.id).first()
        closed = bmc is not None and bmc.closed_at is not None
        # preset: whole euros, rounded DOWN (the cents stay unallocated / on the pot)
        amount = (stored[cat.id] if cat.id in stored
                  else float(math.floor(round(brut - cuts.cuts.get(cat.id, 0.0), 6))))
        # effective budget = ceiling + carry_in + net adjustments: only the spending they do not
        # already cover forces the ceiling up (same `remaining` as envelope_cuts)
        carry_in, adjustments = refloat_service.category_offsets(bm, cat, bmc)
        low = max(cat.min_amount or 0.0, get_spent_for_category(bm, cat) - carry_in - adjustments, 0.0)
        # No max_amount: a personal slider is only bounded by what is left to allocate (below).
        if pot.kind == 'personal':
            ceiling = cat.max_amount if cat.max_amount is not None else math.inf
        else:
            # common pot: a slider may go up to twice the envelope's natural size
            ceiling = 2 * max(brut, cat.max_amount or 0.0)
        high = max(ceiling, amount, low)
        result.envelopes[cat.id] = Envelope(amount=amount, auto=cat.id not in stored,
                                            low=low, high=high, locked=frozen or closed)
    if pot.kind == 'common':
        _common_extra_cut(result, cats, pot, bm, frozen)
        for env in result.envelopes.values():
            if env.locked:
                env.high = env.amount
    if pot.kind == 'personal':
        share = monthly_share(rplan) if has_deficit else 0.0
        result.unallocated = disponible - share - sum(e.amount for e in result.envelopes.values())
        room = max(0.0, result.unallocated)
        for env in result.envelopes.values():
            env.high = (max(env.amount, min(env.high, env.amount + room)) if not env.locked
                        else min(env.high, env.amount))
    return result


def _common_extra_cut(result, cats, pot, bm, frozen):
    """Shortfall left by the solidarity caps lowers the auto, unlocked envelopes (one pass,
    prorata of percentage, within amount - low); what cannot be absorbed goes to `unabsorbed`."""
    from app.services import contribution_service as cs
    cplan = cs.get_plan(pot, bm.year, bm.month)
    if cplan is None or cplan.status == 'validated' or frozen:
        return
    shortfall = cs.shortfall_with_dynamic(cplan, sum(e.amount for e in result.envelopes.values()))
    if shortfall <= EPS:
        return
    pct = {c.id: (c.percentage or 0.0) for c in cats}
    eligible = [cid for cid, e in result.envelopes.items()
                if e.auto and not e.locked and e.amount - e.low > EPS and pct[cid] > 0]
    total_pct = sum(pct[cid] for cid in eligible)
    absorbed = 0.0
    for cid in eligible:
        env = result.envelopes[cid]
        cut = min(shortfall * pct[cid] / total_pct, env.amount - env.low)
        new = env.amount - cut
        floored = float(math.floor(round(new, 6)))
        env.amount = floored if floored >= env.low else new   # whole euros when the floor allows
        absorbed += cut
    left = max(0.0, shortfall - absorbed)
    result.unabsorbed += left
    result.extra_unabsorbed = left


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
    # high wins over low if they ever cross (personal room smaller than the spending to cover):
    # a ceiling is never stored above what is left to allocate.
    amount = min(env.high, max(env.low, float(amount)))
    amount = _whole_euros(amount, env.low, env.high)
    plan = _upsert_plan(pot, year, month)
    row = next((c for c in plan.ceilings if c.category_id == category.id), None)
    if row is None:
        db.session.add(EnvelopeCeiling(plan_id=plan.id, category_id=category.id, amount=amount))
    else:
        row.amount = amount
    db.session.commit()
    _recalculate(pot, year, month)


def _whole_euros(amount, low, high):
    """Nearest euro of a clamped amount, kept inside [low, high] (high wins if they cross)."""
    value = float(round(amount))
    if value > high:
        value = float(math.floor(round(high, 6)))
    if value < low:
        value = float(math.ceil(round(low, 6)))
    return value if value <= high else min(high, amount)


def _upsert_plan(pot, year, month):
    plan = get_plan(pot, year, month)
    if plan is None:
        plan = EnvelopePlan(pot_id=pot.id, year=year, month=month)
        db.session.add(plan)
        db.session.flush()
    return plan


def freeze(pot, year, month, commit=True):
    """Store the current amount of EVERY dynamic envelope and mark the month validated.
    No recalculation: the amounts are the ones already allocated."""
    bm = get_or_create_budget_month(pot.id, year, month)
    amounts = {cid: e.amount for cid, e in report(bm).envelopes.items()}
    plan = _upsert_plan(pot, year, month)
    rows = {c.category_id: c for c in plan.ceilings}
    for cid, amount in amounts.items():
        if cid in rows:
            rows[cid].amount = amount
        else:
            db.session.add(EnvelopeCeiling(plan_id=plan.id, category_id=cid, amount=amount))
    plan.validated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if commit:
        db.session.commit()


def unfreeze(pot, year, month, commit=True):
    """Reopen the month; the stored amounts stay (they become sliders). No recalculation."""
    plan = get_plan(pot, year, month)
    if plan is None:
        return
    plan.validated_at = None
    if commit:
        db.session.commit()


def personal_waiting(pot, year, month):
    """A personal plan waits for the common plan to be validated (multi-member J6 only)."""
    from app.models.user import User
    from app.services import contribution_service as cs
    if User.query.filter_by(household_id=pot.household_id).count() < 2:
        return False
    common = cs._common_of(pot)
    if common is None or not cs.j6_started(common, year, month):
        return False
    cplan = cs.get_plan(common, year, month)
    return cplan is None or cplan.status != 'validated'


def validate_personal(pot, year, month):
    if pot.kind != 'personal':
        raise ValueError('not a personal pot')
    if personal_waiting(pot, year, month):
        raise CeilingError('waiting')
    if is_frozen(pot, year, month):
        raise CeilingError('frozen')
    recalculate_budget_month(get_or_create_budget_month(pot.id, year, month))
    freeze(pot, year, month)
    _recalculate(pot, year, month)


def reopen_personal(pot, year, month):
    if not is_frozen(pot, year, month):
        raise CeilingError('not_frozen')
    unfreeze(pot, year, month)
    _recalculate(pot, year, month)


def reset(pot, year, month):
    """Forget the stored sliders (back to the preset), except those pinned on closed envelopes.
    Refused once the month is frozen."""
    plan = get_plan(pot, year, month)
    if plan is None:
        return
    if plan.validated_at is not None:
        raise CeilingError('frozen')
    bm = BudgetMonth.query.filter_by(pot_id=pot.id, year=year, month=month).first()
    closed = []
    if bm is not None:
        closed = [r.category_id for r in BudgetMonthCategory.query.filter(
            BudgetMonthCategory.budget_month_id == bm.id,
            BudgetMonthCategory.closed_at.isnot(None))]
    q = EnvelopeCeiling.query.filter_by(plan_id=plan.id)
    if closed:
        q = q.filter(EnvelopeCeiling.category_id.notin_(closed))
    q.delete(synchronize_session=False)
    db.session.expire(plan)
    db.session.commit()
