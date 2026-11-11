from urllib.parse import urlparse
from flask import Blueprint, abort, redirect, request, session, url_for
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Pot
from app.services.access_service import can_access_pot

pots_bp = Blueprint('pots', __name__, url_prefix='/pots')


def _safe_referrer():
    ref = request.referrer
    if ref and urlparse(ref).netloc == request.host:
        return ref
    return url_for('dashboard.index')


@pots_bp.route('/<int:pot_id>/select', methods=['POST'])
@login_required
def select(pot_id):
    pot = db.session.get(Pot, pot_id)
    if not can_access_pot(current_user, pot):
        abort(404)
    session['current_pot_id'] = pot.id
    return redirect(_safe_referrer())
