import datetime
import re
from dataclasses import dataclass

from app.extensions import db
from app.models.bank_account import BankAccount
from app.models.bank_connection import PendingBankMovement
from app.models.category import Category
from app.models.operation import Operation
from app.models.pot import Pot
from app.models.user import User
from app.services.budget_service import (
    get_or_create_budget_month, recalculate_budget_month, target_budget_month,
)
from app.services.access_service import can_access_pot
from app.services.reconcile_service import candidates_for, eligible_entries
from app.services.income_service import income_budget_month, record_income, remove_income

CLASSIFICATIONS = ('category', 'income', 'refund', 'internal', 'off_budget', 'contribution')


class ClassificationError(Exception):
    pass


@dataclass
class Suggestion:
    classification: str
    category_id: int | None


def normalize_label(label):
    """Uppercase, drop digits and punctuation, collapse spaces."""
    cleaned = re.sub(r'[\W\d_]+', ' ', (label or '').upper())
    return ' '.join(cleaned.split())


def _base_pot(op):
    """Pot the operation belongs to when it is not classified on a common category."""
    return op.bank_account.pot if op.bank_account is not None else op.pot


def _pot_for_category(op, base_pot, category):
    """Pot the operation must end up in for `category`, or raise."""
    if category is None:
        raise ClassificationError('Une catégorie est requise.')
    if category.pot_id == base_pot.id:
        return base_pot
    if op.bank_account is not None and base_pot.kind == 'personal':
        pot = db.session.get(Pot, category.pot_id)
        if pot is not None and pot.kind == 'common' and pot.household_id == base_pot.household_id:
            return pot
    raise ClassificationError('Catégorie non autorisée pour cette opération.')


def _recalculate(pot_ids, on):
    for pot_id in pot_ids:
        recalculate_budget_month(get_or_create_budget_month(pot_id, on.year, on.month))


def classify(op, user, classification, category=None, contributor=None):
    if classification not in CLASSIFICATIONS:
        raise ClassificationError('Classification inconnue.')
    base_pot = _base_pot(op)
    target_pot = base_pot
    restricted = not can_access_pot(user, base_pot)
    if restricted and classification not in ('category', 'refund'):
        raise ClassificationError('Classification non autorisée pour cette opération.')
    if classification in ('category', 'refund'):
        wanted = 'debit' if classification == 'category' else 'credit'
        if op.direction != wanted:
            raise ClassificationError('Classification incompatible avec le sens de l\'opération.')
        target_pot = _pot_for_category(op, base_pot, category)
        if restricted and target_pot.kind != 'common':
            raise ClassificationError('Catégorie non autorisée pour cette opération.')
    elif classification == 'income' and op.direction != 'credit':
        raise ClassificationError('Seul un crédit peut être un revenu.')
    elif classification == 'contribution':
        if not contribution_allowed(op, user):
            raise ClassificationError('Contribution possible seulement sur un crédit du compte commun.')
        if contributor is None or contributor.household_id != base_pot.household_id:
            raise ClassificationError('Membre du foyer requis.')

    old_pot_id = op.pot_id
    income_months = []
    if op.income is not None and classification != 'income':
        old = op.income
        income_months.append((old.pot, income_budget_month(old.pot, old.date)))
        remove_income(old, commit=False)
        db.session.flush()
        db.session.refresh(op)

    op.category_id = category.id if classification in ('category', 'refund') else None
    op.pot_id = target_pot.id
    op.classification = classification
    op.contributor_id = contributor.id if classification == 'contribution' else None

    if classification == 'income' and op.income is None:
        on = op.bank_date or op.date
        record_income(target_pot, user, op.amount, op.bank_label or op.label, on,
                      operation=op, commit=False)
        income_months.append((target_pot, income_budget_month(target_pot, on)))
    db.session.commit()

    _recalculate({old_pot_id, op.pot_id}, op.date)
    for pot, (year, month) in income_months:
        recalculate_budget_month(get_or_create_budget_month(pot.id, year, month))
    if classification == 'income' and target_pot.kind == 'personal':
        _prepare_household_plan(target_pot, [ym for pot, ym in income_months if pot.id == target_pot.id])
    return op


def _prepare_household_plan(personal_pot, salary_months=()):
    """A salary just landed: the household's J6 plan may now be ready to prepare, or a draft is stale."""
    from flask import current_app
    from app.services.contribution_service import get_plan, measure_caps, prepare_if_ready, recalculate_household
    common = Pot.query.filter_by(household_id=personal_pot.household_id, kind='common').first()
    if common is None:
        return
    try:
        today = datetime.date.today()
        prepare_if_ready(common, today)
        for year, month in salary_months:
            plan = get_plan(common, year, month)
            if plan is not None and plan.status == 'draft':
                measure_caps(plan, today)
                recalculate_household(common, year, month)
    except Exception:
        db.session.rollback()
        current_app.logger.exception('J6 plan preparation failed for pot %s', common.id)


def contribution_allowed(op, user):
    """A « Contribution » is a credit landing on a common-pot account."""
    base_pot = _base_pot(op)
    return (op.direction == 'credit' and op.bank_account is not None and base_pot.kind == 'common'
            and can_access_pot(user, base_pot))


def contribution_received(common_pot, user, year, month):
    """Contribution transfers received on the common pot from `user` for the month (year, month).

    A J6 month runs from its plan's creation to the next plan's (transfers are made before the
    budget month starts); other months use their budget-month window (J4 supplements).
    """
    from app.services.budget_service import budget_month_start
    from app.services.contribution_service import _next, get_plan
    plan = get_plan(common_pot, year, month)
    end = None
    if plan is not None:
        start = plan.created_at.date()
        following = get_plan(common_pot, *_next(year, month))
        if following is not None:
            end = following.created_at.date()
    else:
        start = budget_month_start(common_pot, year, month)
        end = budget_month_start(common_pot, *_next(year, month))
    query = db.session.query(db.func.sum(Operation.amount)).filter(
        Operation.pot_id == common_pot.id,
        Operation.classification == 'contribution',
        Operation.contributor_id == user.id,
        Operation.date >= start,
    )
    if end is not None:
        query = query.filter(Operation.date < end)
    return float(query.scalar() or 0.0)


def suggest(op):
    """Classification of the latest other operation in the pot with the same label and direction."""
    key = normalize_label(op.bank_label or op.label)
    if not key:
        return None
    excluded = ['contribution']  # needs a contributor: never one-click
    if op.direction == 'credit' and op.pot is not None and op.pot.kind == 'common':
        from app.services.contribution_service import j6_started
        if j6_started(op.pot, *target_budget_month(datetime.date.today(), op.pot.income_start_day or 1)):
            excluded.append('income')  # from J6 on, credits on the common pot are contributions
    candidates = Operation.query.filter(
        Operation.pot_id == op.pot_id,
        Operation.id != op.id,
        Operation.direction == op.direction,
        Operation.classification.isnot(None),
        Operation.classification.notin_(excluded),
    ).order_by(db.func.coalesce(Operation.bank_date, Operation.date).desc(), Operation.id.desc()).all()
    for cand in candidates:
        if normalize_label(cand.bank_label or cand.label) == key:
            return Suggestion(cand.classification, cand.category_id)
    return None


TABS = ('a-classer', 'en-attente', 'rapproches')
PENDING_OLD_DAYS = 10
RECONCILED_DAYS = 30


class _CategoryGroups:
    """Per-call cache of the category choices offered to a viewer, one query per pot."""

    def __init__(self, user):
        self.user = user
        self._cats = {}
        self._common = {}
        self._groups = {}

    def _categories(self, pot_id):
        if pot_id not in self._cats:
            self._cats[pot_id] = Category.query.filter_by(pot_id=pot_id).order_by(Category.name).all()
        return self._cats[pot_id]

    def _common_pot(self, household_id):
        if household_id not in self._common:
            self._common[household_id] = Pot.query.filter_by(household_id=household_id, kind='common').first()
        return self._common[household_id]

    def for_op(self, op):
        """[(heading|None, [categories])] for `op`; never exposes a pot the viewer cannot access."""
        base = _base_pot(op)
        if base.id not in self._groups:
            common = self._common_pot(base.household_id)
            groups = []
            if can_access_pot(self.user, base):
                groups.append((None, self._categories(base.id)))
                if base.kind == 'personal' and common is not None:
                    groups.append(('Commun', self._categories(common.id)))
            elif common is not None:
                groups.append((None, self._categories(common.id)))
            self._groups[base.id] = [(t, c) for t, c in groups if c]
        return self._groups[base.id]


def build_panel(pot, tab, user):
    """Plain data for the pointage panel of `pot`: {'tab', 'counts', 'rows'}."""
    tab = tab if tab in TABS else TABS[0]
    base = Operation.query.filter_by(pot_id=pot.id)
    day = db.func.coalesce(Operation.bank_date, Operation.date)
    to_classify_q = (base.filter(Operation.bank_reference.isnot(None), Operation.classification.is_(None))
                     .order_by(day, Operation.id))
    pending_q = (base.filter(Operation.bank_reference.is_(None), Operation.classification == 'category')
                 .order_by(Operation.date, Operation.id))
    today = datetime.date.today()
    reconciled_q = (base.filter(Operation.bank_reference.isnot(None), Operation.classification.isnot(None),
                                Operation.bank_date >= today - datetime.timedelta(days=RECONCILED_DAYS))
                    .order_by(Operation.bank_date.desc(), Operation.id.desc()))
    counts = {'a-classer': to_classify_q.count(), 'en-attente': pending_q.count(),
              'rapproches': reconciled_q.count()}
    groups = _CategoryGroups(user)
    members = User.query.filter_by(household_id=pot.household_id).order_by(User.id).all()
    rows = []
    if tab == 'a-classer':
        for op in to_classify_q.all():
            op_groups = groups.for_op(op)
            candidates = candidates_for(op) if op.direction == 'debit' else []
            sug = suggest(op)
            sug_cat = None
            if sug is not None and sug.category_id is not None:
                sug_cat = next((c for _, cats in op_groups for c in cats if c.id == sug.category_id), None)
                if sug_cat is None:
                    sug = None
            entries = eligible_entries(op, any_amount=True) if op.direction == 'debit' else []
            rows.append(dict(op=op, groups=op_groups, entries=entries,
                             restricted=not can_access_pot(user, _base_pot(op)),
                             candidates=candidates if len(candidates) > 1 else [],
                             suggestion=sug, suggestion_category=sug_cat,
                             refloat=contribution_allowed(op, user)))
        rows.sort(key=lambda r: not r['candidates'])  # conflicts first (stable)
    elif tab == 'en-attente':
        movements = [m for m in to_classify_q.all() if m.direction == 'debit']
        eligible = {m.id: {e.id for e in eligible_entries(m, any_amount=True)} for m in movements}
        for op in pending_q.all():
            rows.append(dict(op=op, old=(today - op.date).days > PENDING_OLD_DAYS,
                             movements=[m for m in movements if op.id in eligible[m.id]]))
    else:
        for op in reconciled_q.all():
            restricted = not can_access_pot(user, _base_pot(op))
            rows.append(dict(op=op, groups=groups.for_op(op), restricted=restricted,
                             can_unlink=op.entered_amount is not None and not restricted,
                             refloat=contribution_allowed(op, user)))
    pending_bank = []
    if tab == 'a-classer' and can_access_pot(user, pot):
        pending_bank = (PendingBankMovement.query.join(BankAccount)
                        .filter(BankAccount.pot_id == pot.id, BankAccount.archived.is_(False))
                        .order_by(PendingBankMovement.date.desc(), PendingBankMovement.id).all())
    return {'tab': tab, 'counts': counts, 'rows': rows, 'pending_bank': pending_bank, 'members': members}


def recategorize_entry(op, category):
    """Move a manual entry (no bank movement) to another category of its own pot, or raise."""
    if op.is_bank_movement or op.classification != 'category':
        raise ClassificationError('Seule une saisie manuelle peut changer de catégorie ici.')
    if category is None or category.pot_id != op.pot_id:
        raise ClassificationError('Catégorie non autorisée pour cette opération.')
    op.category_id = category.id
    db.session.commit()
    _recalculate({op.pot_id}, op.date)


def category_detail(category, year, month, user):
    """Plain data for the detail window of a gauge: {'category', 'rows', 'total'}."""
    from app.services.budget_service import spent_operations
    groups = _CategoryGroups(user)
    rows = []
    for op in spent_operations(category, year, month):
        if op.is_bank_movement:
            rows.append(dict(op=op, bank=True, groups=groups.for_op(op),
                             restricted=not can_access_pot(user, _base_pot(op))))
        else:
            rows.append(dict(op=op, bank=False, groups=[(None, groups._categories(op.pot_id))],
                             restricted=False))
    total = sum(-r['op'].amount if r['op'].direction == 'credit' else r['op'].amount for r in rows)
    return {'category': category, 'rows': rows, 'total': total}
