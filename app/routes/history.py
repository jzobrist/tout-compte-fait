from datetime import date
from flask import Blueprint, render_template
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Category
from app.models.budget import BudgetMonth
from app.models.operation import Operation
from app.services.access_service import get_current_pot
from app.services.budget_service import get_category_effective_budget, get_spent_for_category
from flask_wtf import FlaskForm
from app.services.alert_service import MONTHS_FR

history_bp = Blueprint('history', __name__, url_prefix='/history')

@history_bp.route('')
@login_required
def index():
    today = date.today()
    months = []
    y, m = today.year, today.month
    pot_id = get_current_pot().id
    for _ in range(12):
        bm = BudgetMonth.query.filter_by(
            pot_id=pot_id, year=y, month=m
        ).first()
        if bm:
            cats = Category.query.filter_by(pot_id=pot_id).all()
            cat_data = []
            for cat in cats:
                effective = get_category_effective_budget(bm, cat)
                spent = get_spent_for_category(bm, cat)
                cat_data.append({
                    'category': cat,
                    'effective': effective,
                    'spent': spent,
                    'surplus': effective - spent,
                })
            operations = (
                Operation.query
                .filter_by(pot_id=pot_id)
                .filter(Operation.classification.isnot(None))
                .filter(db.extract('year', Operation.date) == y)
                .filter(db.extract('month', Operation.date) == m)
                .order_by(Operation.date.desc())
                .all()
            )
            months.append({
                'bm': bm,
                'label': f"{MONTHS_FR[m - 1].capitalize()} {y}",
                'cat_data': cat_data,
                'operations': operations,
                'total_income': bm.total_income,
                'total_spent': sum(d['spent'] for d in cat_data),
                'total_surplus': sum(d['surplus'] for d in cat_data),
            })
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return render_template('history/index.html', months=months, csrf_form=FlaskForm())
