from datetime import date
from flask import flash, Blueprint, render_template, request, redirect, url_for, abort
from flask_login import login_required, current_user
from app.services.access_service import get_current_pot, pot_from_form_or_404, load_pot_object_or_404
from app.models.income import Income
from app.routes.forms import IncomeForm
from app.services.income_service import record_income, remove_income

incomes_bp = Blueprint('incomes', __name__, url_prefix='/incomes')

@incomes_bp.route('', methods=['GET', 'POST'])
@login_required
def index():
    form = IncomeForm()
    today = date.today()
    if form.validate_on_submit():
        pot = pot_from_form_or_404(form.pot_id.data)
        record_income(pot, current_user, form.amount.data, form.label.data, today)
        return redirect(url_for('dashboard.index'))
    pot = get_current_pot()
    form.pot_id.data = pot.id
    incomes = Income.query.filter_by(
        pot_id=pot.id
    ).order_by(Income.date.desc()).limit(50).all()
    return render_template('incomes/index.html', form=form, incomes=incomes)

@incomes_bp.route('/<int:income_id>/delete', methods=['POST'])
@login_required
def delete(income_id):
    income = load_pot_object_or_404(Income, income_id)
    if income.operation_id is not None:
        flash('Ce revenu vient d\'un mouvement bancaire : reclassez-le depuis le pointage.', 'warning')
        return redirect(url_for('incomes.index'))
    remove_income(income)
    return redirect(url_for('incomes.index'))
