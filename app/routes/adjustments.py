from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, abort
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Category
from app.models.adjustment import BudgetAdjustment
from app.routes.forms import AdjustmentForm
from app.services.access_service import accessible_pots, get_current_pot, load_pot_object_or_404
from app.services.budget_service import get_or_create_budget_month, target_budget_month

adjustments_bp = Blueprint('adjustments', __name__, url_prefix='/adjustments')

def _adjustment_form(all_accessible=False):
    form = AdjustmentForm()
    # POST validates against categories of every accessible pot, then the same-pot check
    # decides (400); the displayed form only offers the current pot.
    query = Category.query
    if all_accessible:
        query = query.filter(Category.pot_id.in_([p.id for p in accessible_pots(current_user)]))
    else:
        query = query.filter_by(pot_id=get_current_pot().id)
    cats = query.order_by(Category.name).all()
    form.from_category_id.choices = [(c.id, c.name) for c in cats]
    form.to_category_id.choices = [(c.id, c.name) for c in cats]
    return form, cats

@adjustments_bp.route('/form')
@login_required
def form():
    """HTMX: return adjustment form fragment."""
    f, _ = _adjustment_form()
    return render_template('adjustments/_form.html', form=f)

@adjustments_bp.route('', methods=['POST'])
@login_required
def add():
    f, _ = _adjustment_form(all_accessible=True)
    if f.validate_on_submit():
        from_cat = load_pot_object_or_404(Category, f.from_category_id.data)
        to_cat = load_pot_object_or_404(Category, f.to_category_id.data)
        if from_cat.pot_id != to_cat.pot_id:
            abort(400)
        today = date.today()
        pot = from_cat.pot
        year, month = target_budget_month(today, pot.income_start_day or 1)
        bm = get_or_create_budget_month(pot.id, year, month)
        adj = BudgetAdjustment(
            budget_month_id=bm.id,
            user_id=current_user.id,
            from_category_id=f.from_category_id.data,
            to_category_id=f.to_category_id.data,
            amount=f.amount.data,
        )
        db.session.add(adj)
        db.session.commit()
    return redirect(url_for('dashboard.index'))
