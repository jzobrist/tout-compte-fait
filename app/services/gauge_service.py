"""Dashboard gauges: state of each category's gauge and the order they are shown in."""
from app.models import Category
from app.models.budget import BudgetMonthCategory
from app.services.budget_service import compute_daily_metrics, get_spent_for_category

FIXED_TYPES = ('fixed_auto', 'fixed_amount')


def gauge_state(spent, effective, fixed, closed):
    """(state, pct_raw): 'danger' > 'done' > 'warn' > 'ok'; amounts compared to the cent."""
    spent, effective = round(spent, 2), round(effective, 2)
    if effective > 0:
        pct_raw = spent / effective * 100
    elif spent > 0:
        pct_raw = 101  # spending on a 0 € envelope: fully overspent
    else:
        pct_raw = 0
    if spent > effective:
        return 'danger', pct_raw
    if closed or (fixed and effective > 0 and spent == effective):
        return 'done', pct_raw
    return ('warn' if pct_raw > 80 else 'ok'), pct_raw


def sort_key(item):
    """Overspent first, then in progress (most filled first), then done."""
    if item['state'] == 'danger':
        return (0, 0)
    if item['state'] == 'done':
        return (2, 0)
    return (1, -item['pct_raw'])


def build_gauges(bm, current_day, days_in_month):
    """One gauge per category of the budget month's pot, in priority order."""
    closed_ids = {bmc.category_id for bmc in BudgetMonthCategory.query.filter(
        BudgetMonthCategory.budget_month_id == bm.id,
        BudgetMonthCategory.closed_at.isnot(None))}
    gauges = []
    for cat in Category.query.filter_by(pot_id=bm.pot_id).order_by(Category.id).all():
        spent = get_spent_for_category(bm, cat)
        metrics = compute_daily_metrics(bm, cat, current_day, days_in_month, spent)
        closed = cat.id in closed_ids
        state, pct_raw = gauge_state(metrics['spent'], metrics['effective'],
                                     cat.type in FIXED_TYPES, closed)
        gauges.append({'category': cat, 'metrics': metrics, 'type': cat.type, 'state': state,
                       'pct_raw': pct_raw, 'pct': min(pct_raw, 100), 'closed': closed})
    return sorted(gauges, key=sort_key)
