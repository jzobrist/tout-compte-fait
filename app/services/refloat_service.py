"""Remise à flot (J4): frozen monthly deficit, member transfer supplements, envelope cuts.

A plan exists per pot and budget month once the deficit has been measured. The deficit is
re-measured every month, so whatever was not absorbed shows up again in the next measurement.
"""
import logging
import math
from dataclasses import dataclass, field

from app.extensions import db
from app.models.budget import BudgetMonthCategory
from app.models.category import Category
from app.models.pot import Pot
from app.models.refloat import RefloatPlan
from app.models.user import User
from app.services.budget_service import (
    budget_month_start, dynamic_brut, get_category_effective_budget, get_or_create_budget_month,
    get_spent_for_category, pot_disponible, recalculate_budget_month, target_budget_month,
)

MONTHS_MIN, MONTHS_MAX = 1, 6
SPLIT_DEFAULT = 0.5
SPLIT_STEP = 0.05
EPS = 1e-9


@dataclass
class MemberTransfer:
    user: User
    requested: float
    capped: bool
    received: float
    due: float

logger = logging.getLogger(__name__)


@dataclass
class CutReport:
    cuts: dict = field(default_factory=dict)
    capacity: dict = field(default_factory=dict)
    unabsorbed: float = 0.0


def _prev(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _next(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


def get_plan(pot, year, month):
    return RefloatPlan.query.filter_by(pot_id=pot.id, year=year, month=month).first()


def _common_pot(household_id):
    return Pot.query.filter_by(household_id=household_id, kind='common').first()


def _measurable(pot, bm):
    """Income received and every account balance fetched since the budget month started."""
    from app.services.balance_service import pot_balance
    if bm.total_income <= 0:
        return False
    balance = pot_balance(pot)
    if not balance.known or balance.as_of is None:
        return False
    return balance.as_of.date() >= budget_month_start(pot, bm.year, bm.month)


def ensure_plan(pot, bm, today):
    """Plan of this budget month, created (deficit measured and frozen) when measurable."""
    from app.services.balance_service import free_money
    from app.services.contribution_service import j6_started
    plan = get_plan(pot, bm.year, bm.month)
    if plan is not None:
        return plan
    if pot.kind == 'common' and j6_started(pot, bm.year, bm.month):
        return None
    if not _measurable(pot, bm):
        return None
    recalculate_budget_month(bm)
    fm = free_money(pot, bm)
    prev = get_plan(pot, *_prev(bm.year, bm.month))
    months = max(MONTHS_MIN, prev.months - 1) if prev is not None and prev.deficit > 0 else MONTHS_MIN
    split = prev.split if prev is not None else SPLIT_DEFAULT
    plan = RefloatPlan(pot_id=pot.id, year=bm.year, month=bm.month,
                       deficit=max(0.0, -fm.value), months=months, split=split)
    db.session.add(plan)
    db.session.commit()
    _recalculate_plan(plan)
    return plan


def monthly_share(plan):
    return plan.deficit / plan.months if plan.months else plan.deficit


def matching_personal_month(common_pot, personal_pot, year, month):
    """Personal budget month containing the start of the common budget month."""
    start = budget_month_start(common_pot, year, month)
    p_year, p_month = target_budget_month(start, personal_pot.income_start_day or 1)
    return get_or_create_budget_month(personal_pot.id, p_year, p_month)


def _matching_common_month(personal_pot, year, month):
    """(year, month) of the common budget month whose matching personal month is (year, month)."""
    common = _common_pot(personal_pot.household_id)
    if common is None:
        return None, None
    start = budget_month_start(personal_pot, year, month)
    first = target_budget_month(start, common.income_start_day or 1)
    for cand in (_prev(*first), first, _next(*first)):
        c_start = budget_month_start(common, *cand)
        if target_budget_month(c_start, personal_pot.income_start_day or 1) == (year, month):
            return common, cand
    return common, None


def member_cap(personal_bm):
    """Most a member can add to the common pot: income − fixed charges − dynamic floors."""
    cats = Category.query.filter_by(pot_id=personal_bm.pot_id).all()
    fixed = sum(c.amount or 0.0 for c in cats if c.type in ('fixed_auto', 'fixed_amount'))
    floors = sum(c.min_amount or 0.0 for c in cats if c.type == 'dynamic')
    return max(0.0, personal_bm.total_income - fixed - floors)


def _transfers(common_plan):
    """(list of MemberTransfer, part of V not covered because of caps)."""
    from app.services.contribution_service import is_j6
    from app.services.pointage_service import contribution_received
    common = common_plan.pot
    if is_j6(common, common_plan.year, common_plan.month):
        return [], 0.0
    members = User.query.filter_by(household_id=common.household_id).order_by(User.id).all()
    v = monthly_share(common_plan) * common_plan.split
    personal_bms = {}
    for u in members:
        personal = Pot.query.filter_by(owner_id=u.id, kind='personal').first()
        if personal is not None:
            personal_bms[u.id] = matching_personal_month(common, personal, common_plan.year,
                                                         common_plan.month)
    total_income = sum(bm.total_income for bm in personal_bms.values())
    result, uncovered = [], 0.0
    for u in members:
        pbm = personal_bms.get(u.id)
        if v <= 0 or not members:
            wanted = 0.0
        elif total_income > 0:
            wanted = v * (pbm.total_income if pbm else 0.0) / total_income
        else:
            wanted = v / len(members)
        cap = member_cap(pbm) if pbm is not None else 0.0
        requested = min(wanted, cap)
        uncovered += wanted - requested
        received = contribution_received(common, u, common_plan.year, common_plan.month)
        result.append(MemberTransfer(u, requested, wanted > cap + EPS, received,
                                     max(0.0, requested - received)))
    return result, uncovered


def member_transfers(common_plan):
    return _transfers(common_plan)[0]


def member_transfer_for(personal_pot, year, month):
    """The owner's MemberTransfer from the common plan matching this personal month, or None."""
    common, cand = _matching_common_month(personal_pot, year, month)
    if cand is None:
        return None
    plan = get_plan(common, *cand)
    if plan is None:
        return None
    return next((t for t in member_transfers(plan) if t.user.id == personal_pot.owner_id), None)


def supplement_requested_for(personal_pot, year, month):
    transfer = member_transfer_for(personal_pot, year, month)
    return transfer.requested if transfer is not None else 0.0


def envelope_part(plan):
    share = monthly_share(plan)
    if plan.pot.kind != 'common':
        return share
    return share * (1.0 - plan.split) + _transfers(plan)[1]


def category_offsets(bm, cat, bmc):
    """(carry_in, net adjustments) of a category this month: effective budget = ceiling + both."""
    if bmc is None:
        return 0.0, 0.0
    return bmc.carry_in, get_category_effective_budget(bm, cat) - bmc.base_allocated


def envelope_cuts(plan, bm):
    """Reduction of each dynamic envelope (auto prorata of pct), within capacity."""
    if plan is None:
        return CutReport()
    target = envelope_part(plan)
    cats = Category.query.filter_by(pot_id=bm.pot_id, type='dynamic').order_by(Category.id).all()
    disponible = pot_disponible(bm)
    total_pct = sum(c.percentage or 0.0 for c in cats)
    report = CutReport()
    for cat in cats:
        brut = dynamic_brut(cat, disponible)
        bmc = BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=cat.id).first()
        if bmc is not None and bmc.closed_at is not None:
            remaining = 0.0
        else:
            carry_in, adjustments = category_offsets(bm, cat, bmc)
            remaining = max(0.0, brut + carry_in + adjustments - get_spent_for_category(bm, cat))
        capacity = max(0.0, min(remaining, brut - (cat.min_amount or 0.0)))
        if total_pct > 0:
            cut = min(capacity, target * (cat.percentage or 0.0) / total_pct)
        else:
            cut = 0.0
        report.capacity[cat.id] = capacity
        report.cuts[cat.id] = cut
    report.unabsorbed = max(0.0, target - sum(report.cuts.values()))
    return report


def _recalculate_plan(plan):
    pot = plan.pot or db.session.get(Pot, plan.pot_id)
    if pot.kind == 'common':
        from app.services.contribution_service import is_j6, recalculate_household
        if is_j6(pot, plan.year, plan.month):
            recalculate_household(pot, plan.year, plan.month)
            return
    recalculate_budget_month(get_or_create_budget_month(pot.id, plan.year, plan.month))
    if pot.kind == 'common':
        for u in User.query.filter_by(household_id=pot.household_id).all():
            personal = Pot.query.filter_by(owner_id=u.id, kind='personal').first()
            if personal is not None:
                recalculate_budget_month(matching_personal_month(pot, personal, plan.year, plan.month))


def set_settings(plan, months, split):
    from app.services import ceiling_service
    if ceiling_service.is_frozen(plan.pot, plan.year, plan.month):
        raise ceiling_service.CeilingError('frozen')
    plan.months = min(MONTHS_MAX, max(MONTHS_MIN, int(months)))
    if split is not None and plan.pot.kind == 'common':
        split = min(1.0, max(0.0, float(split)))
        plan.split = round(round(split / SPLIT_STEP) * SPLIT_STEP, 2)
    db.session.commit()
    ceiling_service.reset(plan.pot, plan.year, plan.month)
    db.session.refresh(plan)
    _recalculate_plan(plan)


def simulation_view(pot, today, user):
    """Simulation block data for `user` (None once the real plan to come exists)."""
    from app.services import simulation_service as sim
    result = sim.simulate(pot, user, today)
    if result is None:
        return None
    return dict(result=result, forecasts=sim.default_forecasts(user),
                members=User.query.filter_by(household_id=pot.household_id).order_by(User.id).all())


def _contribution_view(pot, today, bm, j6_plan, requested, user):
    view = _contribution_base(pot, today, bm, j6_plan, requested, user)
    view['simulation'] = simulation_view(pot, today, user) if user is not None else None
    return view


def _contribution_base(pot, today, bm, j6_plan, requested, user):
    """J6 block of « Plan du mois » (common pot): plan, waiting state or a link to the prepared month."""
    from app.services import contribution_service as cs
    from app.services.alert_service import month_label
    plan = (cs.get_plan(pot, bm.year, bm.month) if requested else None) or j6_plan
    month = (plan.year, plan.month) if plan is not None else cs.target_month(pot, today)
    if month != (bm.year, bm.month):
        return dict(plan=None, link=month, link_label=month_label(*month))
    members = User.query.filter_by(household_id=pot.household_id).count()
    view = dict(plan=plan, link=None, month=month, label=month_label(*month), waiting=[],
                waiting_sync=False, can_force=False)
    if plan is None:
        waiting = cs.missing_salaries(pot, *month)
        view.update(waiting=waiting, waiting_sync=not waiting,
                    can_force=bool(waiting) and len(waiting) < members)
        return view
    need = cs.need(plan)
    others = sum(a.amount for a in plan.allocations)
    allocations = []
    if plan.position > 0:
        amounts = {a.category_id: a.amount for a in plan.allocations}
        for cat in Category.query.filter_by(pot_id=pot.id, provision=True).order_by(Category.name):
            current = amounts.get(cat.id, 0.0)
            allocations.append((cat, current, round(current + max(0.0, plan.position - others), 2)))
    view.update(
        need=need, position=plan.position, members=cs.member_status(plan), shortfall=cs.shortfall(plan), adjusted=cs.adjusted(plan),
        me_validated=user is not None and cs.is_validated_by(plan, user), allocations=allocations,
        snapshot=cs.snapshot(cs.shares(plan)),
        can_reopen=plan.status == 'validated'
        and today < budget_month_start(pot, *_next(plan.year, plan.month)))
    return view


def _my_contribution(pot, year, month):
    from app.services import contribution_service as cs
    amount = cs.contribution_for(pot, year, month)
    if amount is None:
        return None
    common = _common_pot(pot.household_id)
    plan = cs.get_plan(common, year, month)
    return dict(amount=amount, status='validé' if plan.status == 'validated' else 'provisoire',
                due=cs.due_for(pot, year, month))


def _prepare_j6(pot, today):
    from app.services import contribution_service as cs
    try:
        return cs.prepare_if_ready(pot, today)
    except Exception:
        db.session.rollback()
        logger.exception('J6 plan preparation failed for pot %s', pot.id)
        return cs._current_plan(pot, today)


def build_view(pot, today, year=None, month=None, user=None):
    """Plain data for « Plan du mois » of `pot` (current budget month, or the requested one)."""
    from app.services import contribution_service as cs
    from app.services.balance_service import category_remaining, free_money
    from app.services.budget_service import current_budget_month
    j6_plan = _prepare_j6(pot, today) if pot.kind == 'common' else None
    requested = bool(year and month)
    bm = get_or_create_budget_month(pot.id, year, month) if requested else current_budget_month(pot, today)
    if bm.total_income > 0 or cs.is_j6(pot, bm.year, bm.month):
        recalculate_budget_month(bm)
    plan = ensure_plan(pot, bm, today)
    has_deficit = plan is not None and plan.deficit > 0
    from app.services import ceiling_service
    if pot.kind == 'common':
        ceiling_service.freeze_legacy(pot, bm.year, bm.month)
    ceilings = ceiling_service.report(bm)
    order = {'dynamic': 0, 'fixed_amount': 1, 'fixed_auto': 1}
    cats = sorted(Category.query.filter_by(pot_id=pot.id).all(),
                  key=lambda c: (order.get(c.type, 2), c.name.lower()))
    envelopes = []
    for cat in cats:
        bmc = BudgetMonthCategory.query.filter_by(budget_month_id=bm.id, category_id=cat.id).first()
        row = dict(category=cat, dynamic=cat.type == 'dynamic',
                   effective=get_category_effective_budget(bm, cat),
                   spent=get_spent_for_category(bm, cat),
                   remaining=category_remaining(bm, cat),
                   closed=bmc is not None and bmc.closed_at is not None,
                   floor=cat.min_amount or 0.0)
        env = ceilings.envelopes.get(cat.id)
        if env is not None:
            # whole euros, step 1: min = ceil(low), max = floor(high); crossed bounds = the amount
            lo, hi = math.ceil(round(env.low, 6)), math.floor(round(env.high, 6))
            if lo > hi:
                lo = hi = round(env.amount)
            row['slider'] = dict(min=lo, max=hi, disabled=env.locked,
                                 value=min(hi, max(lo, round(env.amount))))
        envelopes.append(row)
    transfers = member_transfers(plan) if has_deficit and pot.kind == 'common' else []
    my_transfer = (member_transfer_for(pot, bm.year, bm.month) if pot.kind == 'personal' else None)
    my_contribution = _my_contribution(pot, bm.year, bm.month) if pot.kind == 'personal' else None
    contribution = (_contribution_view(pot, today, bm, j6_plan, requested, user)
                    if pot.kind == 'common' else None)
    personal = (dict(waiting=ceiling_service.personal_waiting(pot, bm.year, bm.month),
                     validated=ceilings.frozen, unallocated=ceilings.unallocated)
                if pot.kind == 'personal' else None)
    from app.services.alert_service import alert_types_for
    fm = free_money(pot, bm)
    return dict(pot=pot, bm=bm, fm=fm, balance_alerts=alert_types_for(pot, fm), plan=plan,
                has_deficit=has_deficit,
                share=monthly_share(plan) if plan is not None else 0.0,
                envelope_part=envelope_part(plan) if has_deficit else 0.0,
                j6=pot.kind == 'common' and cs.is_j6(pot, bm.year, bm.month),
                report=ceilings, ceilings=ceilings, frozen=ceilings.frozen, personal=personal,
                envelopes=envelopes, transfers=transfers, my_transfer=my_transfer,
                my_contribution=my_contribution, contribution=contribution,
                year=bm.year, month=bm.month)
