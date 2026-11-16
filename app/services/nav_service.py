"""Navigation state shown on every page. Read-only: runs on each full page render."""
from datetime import date

from app.models import Pot
from app.services import ceiling_service, contribution_service
from app.services.budget_service import target_budget_month


def plan_pending(user, today=None):
    """True when `user` has a validation to do on « Plan du mois » this month: the common plan
    is prepared and not validated by them, or their personal month is open and not waiting."""
    today = today or date.today()
    common = Pot.query.filter_by(household_id=user.household_id, kind='common').first()
    personal = Pot.query.filter_by(owner_id=user.id, kind='personal').first()
    window = common or personal
    if window is None:
        return False
    year, month = target_budget_month(today, window.income_start_day or 1)
    if common is not None:
        plan = contribution_service.get_plan(common, year, month)
        if (plan is not None and plan.status != 'validated'
                and not contribution_service.is_validated_by(plan, user)):
            return True
    if personal is None:
        return False
    return (not ceiling_service.is_frozen(personal, year, month)
            and not ceiling_service.personal_waiting(personal, year, month))
