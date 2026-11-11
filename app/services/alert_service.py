import calendar
from flask import current_app
from datetime import date, datetime, timedelta, timezone
from app.extensions import db
from app.models import Pot, Category, User
from app.models.budget import BudgetMonth
from app.models.alert import AlertLog
from app.services.budget_service import (
    get_or_create_budget_month,
    recalculate_budget_month,
    get_category_effective_budget,
    get_spent_for_category,
    target_budget_month,
)
from app.services.email_service import send_alert_email


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _was_alert_sent_recently(pot_id, category_id, alert_type, days=7):
    """Return True if an alert of this type was sent within the last `days` days."""
    cutoff = _utcnow() - timedelta(days=days)
    return AlertLog.query.filter(
        AlertLog.pot_id == pot_id,
        AlertLog.category_id == category_id,
        AlertLog.alert_type == alert_type,
        AlertLog.sent_at >= cutoff,
    ).first() is not None


def check_alerts_for_pot(pot, budget_month, current_day, days_in_month, spent_by_cat):
    """Detect triggered alerts for all categories in a pot for a given month.

    Returns list of dicts: {'type': 'overspending'|'ahead', 'category_id': int}
    Does NOT send — pure detection.
    """
    if current_day <= 0 or days_in_month <= 0:
        return []
    alerts = []
    cats = Category.query.filter_by(pot_id=pot.id).all()
    for cat in cats:
        # Pace alerts only make sense for dynamic categories.
        # fixed_auto / fixed_amount are lump-sum payments (rent, mortgage, insurance…)
        # and will always look like "overspending" early in the month.
        if cat.type != 'dynamic':
            continue
        effective = get_category_effective_budget(budget_month, cat)
        if effective <= 0:
            continue
        spent = spent_by_cat.get(cat.id, 0.0)
        ideal_cumulated = effective * current_day / days_in_month
        if ideal_cumulated <= 0:
            continue
        ratio = spent / ideal_cumulated
        if ratio > 1.2:
            alerts.append({'type': 'overspending', 'category_id': cat.id})
        elif ratio < 0.5 and spent > 0:
            alerts.append({'type': 'ahead', 'category_id': cat.id})
    return alerts


def alert_recipients(pot):
    """Emails to notify: all household members for the common pot, the owner for a personal pot."""
    if pot.is_common:
        users = User.query.filter_by(household_id=pot.household_id).order_by(User.id).all()
    else:
        users = [pot.owner] if pot.owner else []
    return [u.email for u in users if u.email]


def run_daily_alerts(app):
    """APScheduler job: detect and send alerts for all pots.

    Runs daily at 08:00. Sends at most 1 email per category per week (per alert type).
    """
    with app.app_context():
        today = date.today()
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        from app.services.contribution_service import is_j6, prepare_if_ready
        for common in Pot.query.filter_by(kind='common').all():
            try:
                prepare_if_ready(common, today)
            except Exception:
                db.session.rollback()
                app.logger.exception('Contribution plan preparation failed for pot %s', common.id)
        for pot in Pot.query.all():
            bm_year, bm_month = target_budget_month(today, pot.income_start_day or 1)
            try:
                check_free_money_alerts(pot, today)
            except Exception:
                db.session.rollback()
                app.logger.error('Free money alert check failed for pot %s', pot.id)
            bm = BudgetMonth.query.filter_by(
                pot_id=pot.id,
                year=bm_year,
                month=bm_month,
            ).first()
            if not bm or (bm.total_income == 0 and not is_j6(pot, bm.year, bm.month)):
                continue
            cats = Category.query.filter_by(pot_id=pot.id).all()
            spent_by_cat = {
                cat.id: get_spent_for_category(bm, cat) for cat in cats
            }
            alerts = check_alerts_for_pot(
                pot, bm, today.day, days_in_month, spent_by_cat
            )
            if not alerts:
                continue
            emails = alert_recipients(pot)
            for alert in alerts:
                cat = db.session.get(Category, alert['category_id'])
                if _was_alert_sent_recently(pot.id, cat.id, alert['type']):
                    continue
                if alert['type'] == 'overspending':
                    subject = f"[Compta] ⚠️ Dépassement : {cat.name}"
                    body = (
                        f"Bonjour,\n\n"
                        f"La catégorie '{cat.name}' dépasse son rythme budgétaire.\n"
                        f"Vérifiez vos dépenses sur l'application.\n\n"
                        f"— Compta"
                    )
                else:
                    subject = f"[Compta] ✅ En avance : {cat.name}"
                    body = (
                        f"Bonjour,\n\n"
                        f"Vous êtes bien en avance sur la catégorie '{cat.name}'. Bonne gestion !\n\n"
                        f"— Compta"
                    )
                try:
                    send_alert_email(emails, subject, body)
                except Exception:
                    pass  # Don't let mail failure break the scheduler job
                log = AlertLog(
                    pot_id=pot.id,
                    category_id=cat.id,
                    alert_type=alert['type'],
                )
                db.session.add(log)
            db.session.commit()


# --- J4: argent vraiment libre / découvert ---

FREE_ALERT_TYPES = ('free_negative', 'free_overdraft')
FREE_ALERT_TEXT = {
    'free_negative': ('Argent libre négatif',
                      "L'argent vraiment libre du pot « {pot} » est passé sous 0 : le budget du mois "
                      "ne tient plus."),
    'free_overdraft': ('Découvert autorisé dépassé',
                       "L'argent vraiment libre du pot « {pot} » est passé sous le découvert autorisé : "
                       "des frais bancaires sont en vue."),
}


def alert_types_for(pot, fm):
    """Threshold alerts active for this free money (pure)."""
    from app.models import BankAccount
    if fm.value is None or not fm.income_received:
        return []
    limit = sum(a.overdraft_limit or 0.0
                for a in BankAccount.query.filter_by(pot_id=pot.id, archived=False))
    active = []
    if fm.value < 0:
        active.append('free_negative')
    if fm.value < -limit:
        active.append('free_overdraft')
    return active


def free_money_alerts(pot, today):
    """Threshold alerts active for the pot now (measures the month's deficit first if due)."""
    from app.services.balance_service import free_money
    from app.services.budget_service import current_budget_month
    from app.services.refloat_service import ensure_plan
    bm = current_budget_month(pot, today)
    ensure_plan(pot, bm, today)
    return alert_types_for(pot, free_money(pot, bm))


def check_free_money_alerts(pot, today):
    """Update the per-pot alert state; e-mail each alert that just became active. Returns those."""
    from app.models import PotAlertState
    active = free_money_alerts(pot, today)
    fired = []
    for alert_type in FREE_ALERT_TYPES:
        state = PotAlertState.query.filter_by(pot_id=pot.id, alert_type=alert_type).first()
        if state is None:
            state = PotAlertState(pot_id=pot.id, alert_type=alert_type, active=False)
            db.session.add(state)
        now_active = alert_type in active
        if now_active and not state.active:
            fired.append(alert_type)
        state.active = now_active
    db.session.commit()
    if fired:
        emails = alert_recipients(pot)
        for alert_type in fired:
            title, text = FREE_ALERT_TEXT[alert_type]
            body = (f"Bonjour,\n\n{text.format(pot=pot.name)}\n"
                    f"Ouvrez le « Plan du mois » pour ajuster les enveloppes et la remise à flot.\n\n"
                    f"— Tout Compte Fait")
            try:
                send_alert_email(emails, f"[Tout Compte Fait] {title} : {pot.name}", body)
            except Exception:
                pass  # Don't let mail failure break the sync or the scheduler job
    return fired


# --- J6: contribution plan e-mails ---

MONTHS_FR = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août',
             'septembre', 'octobre', 'novembre', 'décembre']
MONTHS_FR_ABBR = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'août',
                  'sept.', 'oct.', 'nov.', 'déc.']


def month_label(year, month):
    return f'{MONTHS_FR[month - 1]} {year}'


def send_plan_validated_alerts(plan):
    """E-mail the household when the contribution plan is validated (and warn if R is negative)."""
    from app.services.contribution_service import adjusted, member_status
    common = plan.pot
    to = alert_recipients(common)
    label = month_label(plan.year, plan.month)
    lines = [f'Le plan de contribution de {label} est validé.', '']
    lines += [f'{m.user.display_name} : {m.amount:.2f} €' for m in member_status(plan)]
    note = ['', 'Montants ajustés par solidarité'] if adjusted(plan) else []
    mails = [(f'[Tout Compte Fait] Plan de {label} validé', '\n'.join(lines + note))]
    if plan.validated_negative:
        mails.append((
            '[Tout Compte Fait] Plan validé avec un reste à vivre négatif',
            'Le plan a été validé alors que le besoin du commun dépasse ce que vos revenus '
            'permettent : le reste à vivre est négatif. Consultez le plan pour en discuter.'
            + ('\n\nMontants ajustés par solidarité' if note else '')))
    for subject, body in mails:
        try:
            send_alert_email(to, subject, body)
        except Exception:
            current_app.logger.error('Plan validation e-mail failed for plan %s', plan.id)
