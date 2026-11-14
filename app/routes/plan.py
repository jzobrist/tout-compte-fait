import math
from datetime import date

from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Category, Pot
from app.services import ceiling_service, contribution_service, refloat_service, simulation_service
from app.services.ceiling_service import CeilingError
from app.services.contribution_service import ContributionError
from app.services.access_service import can_access_pot
from app.services.balance_service import EnvelopeError, close_envelope, reopen_envelope
from app.services.budget_service import (
    current_budget_month, get_or_create_budget_month, target_budget_month,
)

plan_bp = Blueprint('plan', __name__, url_prefix='/plan')


def _pot_or_404(pot_id):
    pot = db.session.get(Pot, pot_id)
    if not can_access_pot(current_user, pot):
        abort(404)
    return pot


def _category_or_404(pot, category_id):
    cat = db.session.get(Category, category_id)
    if cat is None or cat.pot_id != pot.id:
        abort(404)
    return cat


def _month_or_400(pot, source):
    """(year, month) asked for by the request; only the pot's current or next budget month."""
    year, month = source.get('year', type=int), source.get('month', type=int)
    if year is None and month is None:
        return None, None
    window_pot = pot
    if pot.kind == 'personal':
        # a personal month is the common month with the same (y, m): use the common window
        window_pot = Pot.query.filter_by(household_id=pot.household_id, kind='common').first() or pot
    cur = target_budget_month(date.today(), window_pot.income_start_day or 1)
    nxt = (cur[0] + 1, 1) if cur[1] == 12 else (cur[0], cur[1] + 1)
    if (year, month) not in (cur, nxt):
        abort(400)
    return year, month


def _my_personal_pot():
    """The VIEWER's personal pot: the only personal pot a page or oob block may be built from."""
    return Pot.query.filter_by(owner_id=current_user.id, kind='personal').first()


def _personal_oob(common_view):
    """Out-of-band refresh of section ② from the viewer's own personal pot (never another member's)."""
    mine = _my_personal_pot()
    if mine is None:
        return ''
    view = refloat_service.build_view(mine, date.today(), common_view['year'], common_view['month'],
                                      current_user)
    return render_template('plan/_personal_oob.html', **view)


def _respond(pot, fragment):
    if not request.headers.get('HX-Request'):
        return redirect(url_for('plan.index'))
    year, month = _month_or_400(pot, request.form)
    view = refloat_service.build_view(pot, date.today(), year, month, current_user)
    body = render_template(fragment, **view)
    if pot.kind == 'common':
        body += _personal_oob(view)
    else:
        body += render_template('plan/_personal_validation.html', oob=True, **view)
    return body


def _plan_or_400(pot):
    """Refloat plan of the displayed month (current: measured if needed; next: must exist)."""
    today = date.today()
    year, month = _month_or_400(pot, request.form)
    bm = current_budget_month(pot, today)
    if year is None or (year, month) == (bm.year, bm.month):
        plan = refloat_service.ensure_plan(pot, bm, today)
    else:
        plan = refloat_service.get_plan(pot, year, month)
    if plan is None or plan.deficit <= 0:
        abort(400)
    return plan


def _common_or_404(pot_id):
    pot = _pot_or_404(pot_id)
    if pot.kind != 'common':
        abort(404)
    return pot


@plan_bp.route('')
@login_required
def index():
    mine = _my_personal_pot()
    common = Pot.query.filter_by(household_id=current_user.household_id, kind='common').first()
    if common is None or mine is None:
        abort(404)
    year, month = _month_or_400(common, request.args)
    cv = refloat_service.build_view(common, date.today(), year, month, current_user)
    pv = refloat_service.build_view(mine, date.today(), cv['year'], cv['month'], current_user)
    return render_template('plan/index.html', balance_alerts=cv['balance_alerts'],
                           my_contribution=pv['my_contribution'],
                           common_html=render_template('plan/_section.html', **cv),
                           personal_html=render_template('plan/_section.html', **pv))


@plan_bp.route('/<int:pot_id>/settings', methods=['POST'])
@login_required
def settings(pot_id):
    pot = _pot_or_404(pot_id)
    plan = _plan_or_400(pot)
    months = request.form.get('months', type=int) or plan.months
    split = request.form.get('split', type=int)
    try:
        refloat_service.set_settings(plan, months, split / 100 if split is not None else None)
    except CeilingError:
        abort(400)
    return _respond(pot, 'plan/_envelopes_response.html')


def _toggle(pot_id, category_id, close):
    pot = _pot_or_404(pot_id)
    cat = _category_or_404(pot, category_id)
    year, month = _month_or_400(pot, request.form)
    if year:
        bm = get_or_create_budget_month(pot.id, year, month)
    else:
        bm = current_budget_month(pot, date.today())
    try:
        (close_envelope if close else reopen_envelope)(bm, cat, date.today())
    except EnvelopeError:
        abort(400)
    refloat_service.recalculate_budget_month(bm)
    return _respond(pot, 'plan/_envelopes_response.html')


@plan_bp.route('/<int:pot_id>/envelope/<int:category_id>/close', methods=['POST'])
@login_required
def close(pot_id, category_id):
    return _toggle(pot_id, category_id, True)


@plan_bp.route('/<int:pot_id>/envelope/<int:category_id>/reopen', methods=['POST'])
@login_required
def reopen(pot_id, category_id):
    return _toggle(pot_id, category_id, False)


def _contribution_plan_or_400(pot):
    year, month = _month_or_400(pot, request.form)
    if year is None:
        bm = current_budget_month(pot, date.today())
        year, month = bm.year, bm.month
    plan = contribution_service.get_plan(pot, year, month)
    if plan is None:
        abort(400)
    return plan


@plan_bp.route('/<int:pot_id>/contribution/force', methods=['POST'])
@login_required
def contribution_force(pot_id):
    pot = _common_or_404(pot_id)
    _month_or_400(pot, request.form)
    try:
        contribution_service.prepare_if_ready(pot, date.today(), force=True)
    except (ContributionError, ValueError):
        abort(400)
    return _respond(pot, 'plan/_contribution_response.html')


@plan_bp.route('/<int:pot_id>/contribution/validate', methods=['POST'])
@login_required
def contribution_validate(pot_id):
    pot = _common_or_404(pot_id)
    plan = _contribution_plan_or_400(pot)
    try:
        contribution_service.validate(plan, current_user, date.today(),
                                      seen_snapshot=request.form.get('snapshot'))
    except ContributionError as e:
        if str(e) != 'stale':
            abort(400)
        response = _respond(pot, 'plan/_contribution_response.html')
        if not isinstance(response, str):
            abort(409)
        notice = ('<div class="alert alert-warning small my-2" role="alert">'
                  'Le plan a changé, vérifiez les montants avant de valider.</div>')
        return notice + response, 409
    except ValueError:
        abort(400)
    return _respond(pot, 'plan/_contribution_response.html')


@plan_bp.route('/<int:pot_id>/contribution/reopen', methods=['POST'])
@login_required
def contribution_reopen(pot_id):
    pot = _common_or_404(pot_id)
    plan = _contribution_plan_or_400(pot)
    try:
        contribution_service.reopen(plan, current_user, date.today())
    except (ContributionError, ValueError):
        abort(400)
    return _respond(pot, 'plan/_contribution_response.html')


@plan_bp.route('/<int:pot_id>/allocation/<int:category_id>', methods=['POST'])
@login_required
def allocation(pot_id, category_id):
    pot = _common_or_404(pot_id)
    cat = _category_or_404(pot, category_id)
    plan = _contribution_plan_or_400(pot)
    amount = request.form.get('amount', type=float)
    if amount is None:
        abort(400)
    try:
        contribution_service.set_allocation(plan, cat, amount)
    except (ValueError, ContributionError):
        abort(400)
    return _respond(pot, 'plan/_figures.html')


def _forecast_fields_or_400():
    """[(member_id, amount|None)] from the `salary_<id>` fields; empty means delete the forecast."""
    fields = []
    for key, raw in request.form.items():
        if not key.startswith('salary_'):
            continue
        try:
            member_id = int(key[len('salary_'):])
            raw = raw.strip().replace(',', '.')
            amount = float(raw) if raw else None
        except ValueError:
            abort(400)
        if amount is not None and not math.isfinite(amount):
            abort(400)
        fields.append((member_id, amount))
    return fields


@plan_bp.route('/<int:pot_id>/simulation', methods=['POST'])
@login_required
def simulation(pot_id):
    pot = _common_or_404(pot_id)
    fields = _forecast_fields_or_400()
    try:
        for member_id, amount in fields:
            simulation_service.save_forecast(current_user, member_id, amount)
    except LookupError:
        abort(404)
    except ValueError:
        abort(400)
    if not request.headers.get('HX-Request'):
        return redirect(url_for('plan.index'))
    return render_template('plan/_simulation_result.html',
                           s=refloat_service.simulation_view(pot, date.today(), current_user))


@plan_bp.route('/<int:pot_id>/ceiling/<int:category_id>', methods=['POST'])
@login_required
def ceiling(pot_id, category_id):
    pot = _pot_or_404(pot_id)
    cat = _category_or_404(pot, category_id)
    year, month = _month_or_400(pot, request.form)
    if year is None:
        bm = current_budget_month(pot, date.today())
        year, month = bm.year, bm.month
    amount = request.form.get('amount', type=float)
    if amount is None or not math.isfinite(amount):
        abort(400)
    try:
        ceiling_service.set_ceiling(pot, year, month, cat, amount)
    except (ValueError, CeilingError):
        abort(400)
    return _respond(pot, 'plan/_figures.html')


def _personal_toggle(pot_id, action):
    pot = _pot_or_404(pot_id)
    if pot.kind != 'personal':
        abort(404)
    year, month = _month_or_400(pot, request.form)
    if year is None:
        bm = current_budget_month(pot, date.today())
        year, month = bm.year, bm.month
    try:
        action(pot, year, month)
    except (ValueError, CeilingError):
        abort(400)
    return _respond(pot, 'plan/_personal_response.html')


@plan_bp.route('/<int:pot_id>/ceilings/validate', methods=['POST'])
@login_required
def ceilings_validate(pot_id):
    return _personal_toggle(pot_id, ceiling_service.validate_personal)


@plan_bp.route('/<int:pot_id>/ceilings/reopen', methods=['POST'])
@login_required
def ceilings_reopen(pot_id):
    return _personal_toggle(pot_id, ceiling_service.reopen_personal)
