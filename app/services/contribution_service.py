"""Contribution au commun à reste à vivre égal (J6)."""
from dataclasses import dataclass
import json
import logging
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.budget import BudgetMonth
from app.models.category import Category
from app.models.contribution import (
    ContributionCap, ContributionPlan, ContributionShare, ContributionValidation, ProvisionAllocation,
)
from app.models.pot import Pot
from app.models.refloat import RefloatPlan
from app.models.user import User
from app.services.budget_service import (
    current_budget_month, dynamic_brut, get_or_create_budget_month, recalculate_budget_month,
    spent_for_category, target_budget_month,
)

COMMON_TRANSFER_ROLE = 'common_transfer'
logger = logging.getLogger(__name__)


class ContributionError(Exception):
    pass


@dataclass
class MemberBase:
    user: User
    salary: float
    constrained: float
    salary_known: bool

    @property
    def value(self):
        return self.salary - self.constrained


def _constrained_categories(personal_pot):
    return Category.query.filter(
        Category.pot_id == personal_pot.id, Category.constrained.is_(True),
        Category.type.in_(('fixed_auto', 'fixed_amount'))).all()


def constrained_total(personal_pot):
    """Σ amount of the personal pot's constrained fixed categories."""
    return sum(c.amount or 0.0 for c in _constrained_categories(personal_pot))


def member_base(user, year, month):
    """Salary of the member's personal budget month (same year/month) minus constrained fixed charges."""
    pot = Pot.query.filter_by(owner_id=user.id, kind='personal').one()
    bm = BudgetMonth.query.filter_by(pot_id=pot.id, year=year, month=month).first()
    salary = float(bm.total_income or 0.0) if bm else 0.0
    constrained = constrained_total(pot)
    return MemberBase(user=user, salary=salary, constrained=constrained, salary_known=salary > 0)


def split_equal_rav(bases, need):
    """Water-filling split of `need` so every contributor keeps the same reste à vivre.

    bases: {user_id: base}. Members whose base is below R contribute 0. Rounded to cents;
    the highest user id among the active members absorbs the rounding.
    """
    shares = {uid: 0.0 for uid in bases}
    if need <= 0 or not bases:
        return shares
    active = sorted(bases)
    while True:
        r = (sum(bases[u] for u in active) - need) / len(active)
        low = [u for u in active if bases[u] - r < 0]
        if not low:
            break
        active = [u for u in active if u not in low]
    total = round(need, 2)
    others = 0.0
    for u in active[:-1]:
        shares[u] = round(bases[u] - r, 2) if bases[u] - r > 0 else 0.0
        others += shares[u]
    shares[active[-1]] = round(total - others, 2)
    return shares


def split_with_caps(bases, need, caps):
    """Equal-reste-à-vivre split where each member's share may be capped.

    caps: {user_id: cap or None}; missing/None = no cap. Members whose equal-RAV share
    exceeds their cap are fixed at the cap and the rest is re-split among the others.
    Returns (shares, adjusted, uncovered): `adjusted` is True when a cap bound,
    `uncovered` is the part of `need` nobody could absorb (>= 0).
    """
    shares = {uid: 0.0 for uid in bases}
    active = dict(bases)
    remaining = round(max(need, 0.0), 2)
    adjusted = False
    while active and remaining > 0:
        part = split_equal_rav(active, remaining)
        over = [u for u in active
                if caps.get(u) is not None and part[u] > caps[u]]
        if not over:
            shares.update(part)
            remaining = 0.0
            break
        adjusted = True
        for u in over:
            shares[u] = round(float(max(caps[u], 0.0)), 2)
            remaining = round(remaining - shares[u], 2)
            del active[u]
    return shares, adjusted, round(max(remaining, 0.0), 2)


def shortfall_from_split(bases, shares, uncovered):
    """Amount by which contributors' shares exceed what their bases allow, plus what nobody
    could absorb because of the caps. Shared by the real plan and the simulation."""
    over = sum(max(0.0, shares[u] - bases[u]) for u in shares if shares[u] > 0)
    return round(over + uncovered, 2)


def shortfall_for(bases, need, caps):
    shares, _, uncovered = split_with_caps(bases, need, caps)
    return shortfall_from_split(bases, shares, uncovered)


def member_cap(user, year, month, today, salary_in_balance=None, salary=None):
    """Most the member can transfer to the common pot for month (year, month) without their
    personal accounts going below (authorised overdraft + household margin); None if unknown.

    Projection of the personal balance at the transfer date: balance, plus the salary of M when
    it is not yet in the balance (`salary`, 0 when None), minus the constrained charges still to
    be debited (rest of the current month, plus all of M when M is later) and minus what the
    member still owes the common pot for the current common month. Never creates budget months.
    """
    from app.models.bank_account import BankAccount
    from app.services.balance_service import pot_balance
    pot = Pot.query.filter_by(owner_id=user.id, kind='personal').first()
    if pot is None:
        return None
    balance = pot_balance(pot)
    if not balance.known or balance.as_of is None or balance.as_of.date() < today - timedelta(days=1):
        return None
    bm = BudgetMonth.query.filter_by(pot_id=pot.id, year=year, month=month).first()
    if salary_in_balance is None:
        salary_in_balance = bm is not None and (bm.total_income or 0.0) > 0
    projection = balance.value + (0.0 if salary_in_balance else (salary or 0.0))
    common = _common_of(pot)
    if common is not None and get_plan(common, year, month) is not None:
        # what the member already transferred for M has left the balance: it is part of the cap
        from app.services.pointage_service import contribution_received
        projection += contribution_received(common, user, year, month)

    constrained_cats = _constrained_categories(pot)
    constrained = sum(c.amount or 0.0 for c in constrained_cats)
    current = target_budget_month(today, pot.income_start_day or 1)
    current_bm = bm if current == (year, month) else BudgetMonth.query.filter_by(
        pot_id=pot.id, year=current[0], month=current[1]).first()
    if current_bm is None:
        remaining = constrained
    else:
        remaining = sum(max(0.0, (c.amount or 0.0) - spent_for_category(c, *current))
                        for c in constrained_cats)
    projection -= remaining
    if current < (year, month):   # M is still ahead: its charges come on top of the current month's
        projection -= constrained

    if common is not None:
        current_common = target_budget_month(today, common.income_start_day or 1)
        if current_common != (year, month):   # the plan being capped is not an earlier debt
            projection -= due_for(pot, *current_common) or 0.0

    overdraft = sum(a.overdraft_limit or 0.0 for a in
                    BankAccount.query.filter_by(pot_id=pot.id, archived=False))
    margin = user.household.solidarity_margin or 0.0
    return max(0.0, projection + overdraft - margin)


# --- plan lookup -----------------------------------------------------------------------------

def _prev(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _next(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


def get_plan(common, year, month):
    return ContributionPlan.query.filter_by(pot_id=common.id, year=year, month=month).first()


def is_j6(pot, year, month):
    return pot.kind == 'common' and get_plan(pot, year, month) is not None


def j6_started(common, year, month):
    return ContributionPlan.query.filter(
        ContributionPlan.pot_id == common.id,
        db.or_(ContributionPlan.year < year,
               db.and_(ContributionPlan.year == year, ContributionPlan.month <= month)),
    ).first() is not None


def _members(common):
    return User.query.filter_by(household_id=common.household_id).order_by(User.id).all()


def _common_of(personal_pot):
    return Pot.query.filter_by(household_id=personal_pot.household_id, kind='common').first()


def target_month(common, today):
    c = current_budget_month(common, today)
    return _target_after(common, (c.year, c.month))


def peek_target_month(common, today):
    """Same as `target_month` without creating the current budget month."""
    return _target_after(common, target_budget_month(today, common.income_start_day or 1))


def _target_after(common, cur):
    if get_plan(common, *cur) is None and RefloatPlan.query.filter_by(
            pot_id=common.id, year=cur[0], month=cur[1]).first() is None:
        return cur
    return _next(*cur)


def missing_salaries(common, year, month):
    return [u for u in _members(common) if not member_base(u, year, month).salary_known]


def recalculate_household(common, year, month):
    recalculate_budget_month(get_or_create_budget_month(common.id, year, month))
    for u in _members(common):
        personal = Pot.query.filter_by(owner_id=u.id, kind='personal').first()
        if personal is not None:
            recalculate_budget_month(get_or_create_budget_month(personal.id, year, month))


def _month_flows_in_balance(common, year, month):
    """Net bank movement (credits − debits) of the common accounts dated from the start of the budget
    month: already in today's balance although they belong to the month being planned."""
    from app.models.bank_account import BankAccount
    from app.models.operation import Operation
    from app.services.budget_service import budget_month_start
    start = budget_month_start(common, year, month)
    query = (db.session.query(Operation.direction, db.func.sum(Operation.amount))
             .join(BankAccount, BankAccount.id == Operation.bank_account_id)
             .filter(BankAccount.pot_id == common.id, Operation.bank_reference.isnot(None),
                     db.func.coalesce(Operation.bank_date, Operation.date) >= start))
    previous = get_plan(common, *_prev(year, month))
    if previous is not None:
        # contributions received for a J6 month M-1 (open-ended window, see
        # pointage_service.contribution_received) already lower its transfer_expected in free_money
        query = query.filter(db.or_(db.func.coalesce(Operation.classification, '') != 'contribution',
                                    Operation.date < previous.created_at.date()))
    rows = query.group_by(Operation.direction).all()
    totals = {direction: float(total or 0.0) for direction, total in rows}
    return totals.get('credit', 0.0) - totals.get('debit', 0.0)


def _current_plan(common, today):
    c = current_budget_month(common, today)
    return get_plan(common, c.year, c.month)


def balance_fresh(common, today):
    from app.services.balance_service import pot_balance
    balance = pot_balance(common)
    return balance.known and balance.as_of is not None and balance.as_of.date() >= today - timedelta(days=1)


def measure_position(common, year, month, today):
    """Common free money at the end of M-1 minus the flows of M already in the balance.

    None when the common balance is unknown or stale, or the budget month M-1 does not exist.
    Never creates anything.
    """
    from app.services.balance_service import free_money
    if not balance_fresh(common, today):
        return None
    prev_year, prev_month = _prev(year, month)
    prev_bm = BudgetMonth.query.filter_by(pot_id=common.id, year=prev_year, month=prev_month).first()
    if prev_bm is None:
        return None
    position = free_money(common, prev_bm).value
    if position is None:
        return None
    # the balance already holds the flows of month M (rent, incomes, contributions) when it has started
    return position - _month_flows_in_balance(common, year, month)


def new_refloat_plan(common, year, month, position):
    """The (unsaved) remise à flot plan prepared for month M: deficit measured on `position`,
    months and split inherited from the previous plan."""
    from app.services.refloat_service import MONTHS_MIN, SPLIT_DEFAULT, get_plan as get_refloat
    prev = get_refloat(common, *_prev(year, month))
    months = max(MONTHS_MIN, prev.months - 1) if prev is not None and prev.deficit > 0 else MONTHS_MIN
    split = prev.split if prev is not None else SPLIT_DEFAULT
    return RefloatPlan(pot_id=common.id, year=year, month=month,
                       deficit=max(0.0, -position), months=months, split=split)


def prepare_if_ready(common, today, force=False):
    """Create the plan of the target month once salaries are in and the balance is fresh."""
    from app.services.refloat_service import get_plan as get_refloat
    year, month = target_month(common, today)
    existing = get_plan(common, year, month)
    if existing is not None:
        return existing
    members = _members(common)
    missing = missing_salaries(common, year, month)
    ready = not missing or (force and len(missing) < len(members))
    if not ready:
        return _current_plan(common, today)
    if balance_fresh(common, today):
        get_or_create_budget_month(common.id, *_prev(year, month))
    position = measure_position(common, year, month, today)
    if position is None:
        return _current_plan(common, today)
    plan = ContributionPlan(pot_id=common.id, year=year, month=month, position=position,
                            forced=bool(missing), status='draft',
                            created_at=datetime.combine(today, datetime.min.time()))
    db.session.add(plan)
    if get_refloat(common, year, month) is None:
        db.session.add(new_refloat_plan(common, year, month, position))
    try:
        db.session.flush()
        measure_caps(plan, today, commit=False)   # one transaction: no plan without its caps
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    recalculate_household(common, year, month)
    return plan


def measure_caps(plan, today, commit=True):
    """Replace the plan's caps by a fresh measure of each member's capacity (None = unknown)."""
    for row in list(plan.caps):
        db.session.delete(row)
    db.session.flush()
    for u in _members(plan.pot):
        db.session.add(ContributionCap(plan_id=plan.id, user_id=u.id,
                                       cap=member_cap(u, plan.year, plan.month, today)))
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    db.session.refresh(plan)


# --- amounts ---------------------------------------------------------------------------------

def bases(plan):
    out = {}
    for u in _members(plan.pot):
        b = member_base(u, plan.year, plan.month)
        out[u.id] = b.value
    return out


def _common_fixed(common):
    return sum(c.amount or 0.0 for c in Category.query.filter(
        Category.pot_id == common.id, Category.type.in_(('fixed_auto', 'fixed_amount'))))


def common_disponible(common_bm):
    pot = common_bm.pot or db.session.get(Pot, common_bm.pot_id)
    plan = get_plan(pot, common_bm.year, common_bm.month)
    total = sum(bases(plan).values()) if plan is not None else 0.0
    return max(0.0, total - _common_fixed(pot))


@dataclass
class Need:
    fixed: float
    dynamic: float
    allocations: float
    refloat_v: float
    surplus: float
    incomes: float

    @property
    def total(self):
        return (self.fixed + self.dynamic + self.allocations + self.refloat_v
                - self.surplus - self.incomes)


def need_for(common, year, month, bases, position, allocations, refloat_v):
    """Need of the common pot for month M from explicit inputs (shared by the real plan and
    the simulation). bases: {user_id: base}."""
    bm = BudgetMonth.query.filter_by(pot_id=common.id, year=year, month=month).first()
    fixed = _common_fixed(common)
    disponible = max(0.0, sum(bases.values()) - fixed)
    dynamic = sum(dynamic_brut(c, disponible) for c in
                  Category.query.filter_by(pot_id=common.id, type='dynamic'))
    return Need(fixed=fixed, dynamic=dynamic, allocations=allocations, refloat_v=refloat_v,
                surplus=max(0.0, position), incomes=float(bm.total_income or 0.0) if bm is not None else 0.0)


def need(plan):
    from app.services.refloat_service import get_plan as get_refloat, monthly_share
    rplan = get_refloat(plan.pot, plan.year, plan.month)
    v = monthly_share(rplan) * rplan.split if rplan is not None else 0.0
    return need_for(plan.pot, plan.year, plan.month, bases(plan), plan.position,
                    sum(a.amount for a in plan.allocations), v)


def caps(plan):
    return {c.user_id: c.cap for c in plan.caps}


def split(plan):
    """(shares, adjusted, uncovered) of the live plan, caps included."""
    return split_with_caps(bases(plan), need(plan).total, caps(plan))


def shares(plan):
    if plan.status == 'validated' and plan.shares:
        return {s.user_id: s.amount for s in plan.shares}
    return split(plan)[0]


def adjusted(plan):
    """True when a solidarity cap changed the shares (frozen once validated)."""
    return plan.solidarity_adjusted if plan.status == 'validated' else split(plan)[1]


def expected_contributions(pot, year, month):
    """Σ contributions of a J6 common month (they are the pot's income there), else 0."""
    plan = get_plan(pot, year, month) if pot.kind == 'common' else None
    return float(sum(shares(plan).values())) if plan is not None else 0.0


def shortfall(plan):
    shares_, _, uncovered = split(plan)
    return shortfall_from_split(bases(plan), shares_, uncovered)


def set_allocation(plan, category, amount):
    if category.pot_id != plan.pot_id or not category.provision:
        raise ValueError('Seule une catégorie de provision du commun peut recevoir le surplus.')
    others = sum(a.amount for a in plan.allocations if a.category_id != category.id)
    value = min(max(0.0, float(amount)), max(0.0, plan.position - others))
    row = ProvisionAllocation.query.filter_by(plan_id=plan.id, category_id=category.id).first()
    if value <= 0:
        if row is not None:
            db.session.delete(row)
    elif row is None:
        db.session.add(ProvisionAllocation(plan_id=plan.id, category_id=category.id, amount=value))
    else:
        row.amount = value
    db.session.commit()
    db.session.refresh(plan)
    recalculate_household(plan.pot, plan.year, plan.month)


def contribution_for(personal_pot, year, month):
    common = _common_of(personal_pot)
    plan = get_plan(common, year, month) if common is not None else None
    if plan is None:
        return None
    return shares(plan).get(personal_pot.owner_id, 0.0)


@dataclass
class MemberContribution:
    user: User
    amount: float
    received: float
    due: float
    validated: bool


def member_status(plan):
    """Per member (by user id): contribution, received on the common pot this month, still due."""
    from app.services.pointage_service import contribution_received
    amounts = shares(plan)
    out = []
    for u in _members(plan.pot):
        amount = amounts.get(u.id, 0.0)
        received = contribution_received(plan.pot, u, plan.year, plan.month)
        out.append(MemberContribution(u, amount, received, max(0.0, amount - received),
                                      is_validated_by(plan, u)))
    return out


def due_for(personal_pot, year, month):
    """What the owner of this personal pot still owes the common pot for a J6 month, else None."""
    common = _common_of(personal_pot)
    plan = get_plan(common, year, month) if common is not None else None
    if plan is None:
        return None
    return next((m.due for m in member_status(plan) if m.user.id == personal_pot.owner_id), 0.0)


# --- validation ------------------------------------------------------------------------------

def snapshot(share_map):
    """Canonical JSON of the shares in cents, keys sorted."""
    return json.dumps({str(uid): int(round(v * 100)) for uid, v in sorted(share_map.items())},
                      separators=(',', ':'))


def is_validated_by(plan, user):
    if plan.status == 'validated':
        return True
    current = snapshot(shares(plan))
    return any(v.user_id == user.id and v.snapshot == current for v in plan.validations)


def validate(plan, user, today, seen_snapshot=None):
    """Record `user`'s approval of the current shares. True when this call validated the plan."""
    from app.services.alert_service import send_plan_validated_alerts
    if plan.status == 'validated':
        raise ContributionError('Le plan est déjà validé.')
    current = shares(plan)
    snap = snapshot(current)
    if seen_snapshot is not None and seen_snapshot != snap:
        raise ContributionError('stale')
    row = next((v for v in plan.validations if v.user_id == user.id), None)
    if row is None:
        db.session.add(ContributionValidation(plan_id=plan.id, user_id=user.id, snapshot=snap))
    else:
        row.snapshot = snap
    db.session.commit()
    db.session.refresh(plan)
    if not all(is_validated_by(plan, u) for u in _members(plan.pot)):
        return False
    negative = shortfall(plan) > 0
    for uid, amount in current.items():
        db.session.add(ContributionShare(plan_id=plan.id, user_id=uid, amount=amount))
    plan.solidarity_adjusted = split(plan)[1]
    plan.status = 'validated'
    plan.validated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    plan.validated_negative = negative
    db.session.commit()
    db.session.refresh(plan)
    recalculate_household(plan.pot, plan.year, plan.month)
    send_plan_validated_alerts(plan)
    return True


def reopen(plan, user, today):
    """Unfreeze a validated plan while the budget month is not over."""
    from app.services.budget_service import budget_month_start
    if plan.status != 'validated':
        raise ContributionError("Le plan n'est pas validé.")
    if today >= budget_month_start(plan.pot, *_next(plan.year, plan.month)):
        raise ContributionError('Le mois est terminé : le plan ne peut plus être rouvert.')
    for row in list(plan.shares) + list(plan.validations):
        db.session.delete(row)
    plan.status = 'draft'
    plan.validated_at = None
    plan.validated_negative = False
    plan.solidarity_adjusted = False
    db.session.commit()
    measure_caps(plan, today)
    recalculate_household(plan.pot, plan.year, plan.month)
