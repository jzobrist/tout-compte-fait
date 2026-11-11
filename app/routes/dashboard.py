from datetime import date
import calendar
from flask import Blueprint, render_template, request
from flask_login import login_required, current_user
from app.models import Category
from app.models.budget import BudgetMonth
from app.models.income import Income
from app.extensions import db
from app.services.access_service import get_current_pot, load_pot_object_or_404
from app.services.balance_service import free_money
from app.services.alert_service import free_money_alerts
from app.services.budget_service import (
    get_or_create_budget_month, recalculate_budget_month,
    get_category_effective_budget, get_spent_for_category, compute_daily_metrics, pot_spent_total,
    target_budget_month,
)

dashboard_bp = Blueprint('dashboard', __name__)

@dashboard_bp.route('/')
@login_required
def index():
    today = date.today()
    pot = get_current_pot()
    start_day = pot.income_start_day or 1
    current_bm_year, current_bm_month = target_budget_month(today, start_day)
    try:
        year = int(request.args.get('year', current_bm_year))
        month = int(request.args.get('month', current_bm_month))
        if not (1 <= month <= 12):
            raise ValueError
    except (ValueError, TypeError):
        year, month = current_bm_year, current_bm_month
    days_in_month = calendar.monthrange(year, month)[1]
    current_day = today.day if (year == today.year and month == today.month) else days_in_month

    bm = get_or_create_budget_month(pot.id, year, month)
    from app.services.contribution_service import expected_contributions, is_j6
    if bm.total_income > 0 or is_j6(pot, year, month):
        recalculate_budget_month(bm)

    categories = Category.query.filter_by(pot_id=pot.id).all()
    cat_data = []
    for cat in categories:
        spent = get_spent_for_category(bm, cat)
        metrics = compute_daily_metrics(bm, cat, current_day, days_in_month, spent)
        cat_data.append({'category': cat, 'metrics': metrics, 'type': cat.type})

    # Alert detection — pace alerts only for dynamic categories
    alerts = []
    for item in cat_data:
        if item['category'].type != 'dynamic':
            continue
        m = item['metrics']
        if m['ideal_cumulated'] > 0:
            ratio = m['spent'] / m['ideal_cumulated']
            if ratio > 1.2:
                alerts.append({'category': item['category'], 'type': 'overspending', 'ratio': ratio})
            elif ratio < 0.5 and m['spent'] > 0:
                alerts.append({'category': item['category'], 'type': 'ahead', 'ratio': ratio})

    # Build last 6 months chart data
    chart_data = _build_chart_data(pot, year, month, 6)

    prev_y, prev_m = _prev_month(year, month)
    next_month = _next_month(year, month, current_bm_year, current_bm_month)

    is_current = (year, month) == (current_bm_year, current_bm_month)
    balance_alerts = free_money_alerts(pot, today) if is_current else []
    fm = free_money(pot, bm) if is_current else None

    total_spent = sum(item['metrics']['spent'] for item in cat_data)
    total_income = bm.total_income + expected_contributions(pot, year, month)
    total_remaining = total_income - total_spent

    return render_template('dashboard/index.html',
        bm=bm, total_income=total_income, cat_data=cat_data, alerts=alerts,
        year=year, month=month, today=today,
        month_name=calendar.month_name[month],
        chart_data=chart_data,
        prev_year=prev_y, prev_month=prev_m,
        next_month=next_month,
        total_spent=total_spent,
        total_remaining=total_remaining,
        fm=fm,
        balance_alerts=balance_alerts,
    )

def _prev_month(year, month):
    m = month - 1
    y = year if m > 0 else year - 1
    m = m if m > 0 else 12
    return y, m

def _next_month(year, month, current_bm_year, current_bm_month):
    if (year, month) >= (current_bm_year, current_bm_month):
        return None
    m = month + 1
    y = year if m <= 12 else year + 1
    m = m if m <= 12 else 1
    return (y, m)

def _build_chart_data(pot, year, month, n):
    """Build income vs spending data for last n months."""
    import calendar as cal
    from app.services.contribution_service import expected_contributions
    months = []
    y, m = year, month
    for _ in range(n):
        months.insert(0, (y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1

    labels, incomes_data, spendings_data = [], [], []
    for y_i, m_i in months:
        labels.append(f"{cal.month_abbr[m_i]} {y_i}")
        bm = BudgetMonth.query.filter_by(pot_id=pot.id, year=y_i, month=m_i).first()
        inc = (bm.total_income if bm else 0.0) + expected_contributions(pot, y_i, m_i)
        spent = pot_spent_total(pot.id, y_i, m_i)
        incomes_data.append(inc)
        spendings_data.append(spent)
    return {'labels': labels, 'incomes': incomes_data, 'spendings': spendings_data}


def _detail_month(values, category):
    """(year, month) from request values, default: the category pot's current budget month."""
    pot = category.pot
    year, month = target_budget_month(date.today(), pot.income_start_day or 1)
    try:
        year = int(values.get('year', year))
        month = int(values.get('month', month))
        if not (1 <= month <= 12):
            raise ValueError
    except (ValueError, TypeError):
        year, month = target_budget_month(date.today(), pot.income_start_day or 1)
    return year, month


def render_category_detail(category, values):
    """Detail window fragment of `category`'s gauge for the month given in `values`."""
    from app.services.pointage_service import category_detail
    year, month = _detail_month(values, category)
    detail = category_detail(category, year, month, current_user)
    return render_template('dashboard/_category_detail.html', detail=detail, year=year, month=month,
                           month_name=calendar.month_name[month])


def detail_response_or_none():
    """After an action posted from a detail window (`detail_cat`), its refreshed fragment."""
    cat_id = request.form.get('detail_cat', type=int)
    if not cat_id:
        return None
    return render_category_detail(load_pot_object_or_404(Category, cat_id), request.form)


@dashboard_bp.route('/category/<int:cat_id>/detail')
@login_required
def category_detail_view(cat_id):
    return render_category_detail(load_pot_object_or_404(Category, cat_id), request.args)
