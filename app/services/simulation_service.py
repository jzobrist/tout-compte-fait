"""Simulation du plan de contribution à venir (salaires prévisionnels saisis par un membre).

Même chaîne de calcul que le vrai plan (`contribution_service`), sans rien écrire hormis les
`SalaryForecast` de l'utilisateur.
"""
import math
from dataclasses import dataclass

from app.extensions import db
from app.models.budget import BudgetMonth
from app.models.pot import Pot
from app.models.salary_forecast import SalaryForecast
from app.models.user import User
from app.services import contribution_service as cs
from app.services.alert_service import MONTHS_FR

UNKNOWN_BALANCE = 'Solde du commun inconnu, synchronisez les comptes.'


@dataclass
class Simulation:
    year: int
    month: int
    label: str
    shares: dict
    adjusted: bool
    shortfall: float
    error: str = None


def forecasts(user):
    """Salaries typed by `user`: {member_id: amount}."""
    return {f.member_id: f.amount for f in SalaryForecast.query.filter_by(user_id=user.id)}


def _last_salary(user):
    pot = Pot.query.filter_by(owner_id=user.id, kind='personal').first()
    if pot is None:
        return None
    bm = (BudgetMonth.query.filter(BudgetMonth.pot_id == pot.id, BudgetMonth.total_income > 0)
          .order_by(BudgetMonth.year.desc(), BudgetMonth.month.desc()).first())
    return float(bm.total_income) if bm is not None else None


def default_forecasts(user):
    """Saved salaries; the user's own field falls back to their last pointed salary."""
    out = {}
    own = _last_salary(user)
    if own is not None:
        out[user.id] = own
    out.update(forecasts(user))
    return out


def save_forecast(user, member_id, amount):
    """Store (or, for None/empty, delete) the salary `user` expects for `member_id`."""
    member = db.session.get(User, member_id)
    if member is None or member.household_id != user.household_id:
        raise LookupError('Membre inconnu.')
    if amount is not None and amount != '':
        if not math.isfinite(float(amount)) or float(amount) < 0:
            raise ValueError('Le salaire doit être positif ou nul.')
    row = SalaryForecast.query.filter_by(user_id=user.id, member_id=member_id).first()
    if amount is None or amount == '':
        if row is not None:
            db.session.delete(row)
    elif row is None:
        db.session.add(SalaryForecast(user_id=user.id, member_id=member_id, amount=float(amount)))
    else:
        row.amount = float(amount)
    db.session.commit()


def simulate(common, user, today):
    """Estimated contributions of the next plan to be prepared, or None once it exists."""
    year, month = cs.peek_target_month(common, today)
    if cs.get_plan(common, year, month) is not None:
        return None
    label = f'{MONTHS_FR[month - 1]} {year}'
    position = cs.measure_position(common, year, month, today)
    if position is None:
        return Simulation(year, month, label, {}, False, 0.0, UNKNOWN_BALANCE)

    typed = default_forecasts(user)
    members = [u for u in cs._members(common)
               if Pot.query.filter_by(owner_id=u.id, kind='personal').first() is not None]
    bases = {}
    for u in members:
        pot = Pot.query.filter_by(owner_id=u.id, kind='personal').first()
        bases[u.id] = typed.get(u.id, 0.0) - cs.constrained_total(pot)

    from app.services.refloat_service import get_plan as get_refloat, monthly_share
    rplan = get_refloat(common, year, month) or cs.new_refloat_plan(common, year, month, position)
    refloat_v = monthly_share(rplan) * rplan.split
    total_need = cs.need_for(common, year, month, bases, position, 0.0, refloat_v).total
    caps = {u.id: cs.member_cap(u, year, month, today, salary=typed.get(u.id, 0.0)) for u in members}
    shares, adjusted, uncovered = cs.split_with_caps(bases, total_need, caps)
    shortfall = cs.shortfall_from_split(bases, shares, uncovered)
    return Simulation(year, month, label, cs.round_shares(shares, caps), adjusted, shortfall)
