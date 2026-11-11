from flask import abort, session
from flask_login import current_user
from app.extensions import db
from app.models import Pot


def accessible_pots(user):
    """Common pot first, then the user's own personal pot."""
    pots = Pot.query.filter_by(household_id=user.household_id).order_by(Pot.id).all()
    common = [p for p in pots if p.kind == 'common']
    personal = [p for p in pots if p.kind == 'personal' and p.owner_id == user.id]
    return common + personal


def can_access_pot(user, pot):
    if pot is None or pot.household_id != user.household_id:
        return False
    return pot.kind == 'common' or pot.owner_id == user.id


def get_current_pot():
    pots = accessible_pots(current_user)
    pot_id = session.get('current_pot_id')
    for pot in pots:
        if pot.id == pot_id:
            return pot
    pot = pots[0]
    session['current_pot_id'] = pot.id
    return pot


def pot_from_form_or_404(raw_pot_id):
    """Resolve a hidden `pot_id` form value to an accessible Pot, else 404."""
    try:
        pot = db.session.get(Pot, int(raw_pot_id))
    except (TypeError, ValueError):
        pot = None
    if not can_access_pot(current_user, pot):
        abort(404)
    return pot


def load_pot_object_or_404(model, obj_id):
    obj = db.session.get(model, obj_id)
    if obj is None or not can_access_pot(current_user, obj.pot):
        abort(404)
    return obj
