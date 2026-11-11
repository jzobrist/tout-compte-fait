from app.services.income_service import reassign_incomes
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, IntegerField, HiddenField
from wtforms.validators import DataRequired, Length, EqualTo, NumberRange
from app.extensions import db
from app.routes.forms import HouseholdSettingsForm
from app.services.access_service import get_current_pot, pot_from_form_or_404

settings_bp = Blueprint('settings', __name__, url_prefix='/settings')


class PotSettingsForm(FlaskForm):
    pot_id = HiddenField()
    income_start_day = IntegerField(
        'Jour de début du mois budgétaire',
        validators=[DataRequired(), NumberRange(min=1, max=28)],
    )


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField('Mot de passe actuel', validators=[DataRequired()])
    new_password = PasswordField('Nouveau mot de passe', validators=[DataRequired(), Length(min=8)])
    confirm_password = PasswordField('Confirmer', validators=[DataRequired(), EqualTo('new_password', message='Les mots de passe ne correspondent pas.')])


@settings_bp.route('', methods=['GET'])
@login_required
def index():
    pw_form = ChangePasswordForm()
    csrf_form = FlaskForm()
    pot = get_current_pot()
    pot_form = PotSettingsForm(obj=pot, pot_id=pot.id)
    invite_url = None
    household = current_user.household
    household_form = HouseholdSettingsForm(obj=household)
    if household.invite_code and household.is_invite_valid():
        invite_url = url_for('auth.join', token=household.invite_code, _external=True)
    return render_template('settings/index.html', pw_form=pw_form, csrf_form=csrf_form, pot_form=pot_form, household_form=household_form, pot=pot, invite_url=invite_url)


@settings_bp.route('/pot', methods=['POST'])
@login_required
def update_pot():
    form = PotSettingsForm()
    if form.validate_on_submit():
        pot = pot_from_form_or_404(form.pot_id.data)
        pot.income_start_day = form.income_start_day.data
        db.session.commit()
        reassign_incomes(pot)
        flash(f'Paramètres du pot « {pot.name} » mis à jour.', 'success')
    else:
        for field_errors in form.errors.values():
            for error in field_errors:
                flash(error, 'danger')
    return redirect(url_for('settings.index'))


@settings_bp.route('/household', methods=['POST'])
@login_required
def update_household():
    form = HouseholdSettingsForm()
    if form.validate_on_submit():
        current_user.household.solidarity_margin = form.solidarity_margin.data
        db.session.commit()
        flash('Paramètres du foyer mis à jour.', 'success')
    else:
        for field_errors in form.errors.values():
            for error in field_errors:
                flash(error, 'danger')
    return redirect(url_for('settings.index'))


@settings_bp.route('/password', methods=['POST'])
@login_required
def change_password():
    pw_form = ChangePasswordForm()
    if pw_form.validate_on_submit():
        if not current_user.check_password(pw_form.current_password.data):
            flash('Mot de passe actuel incorrect.', 'danger')
        else:
            current_user.set_password(pw_form.new_password.data)
            db.session.commit()
            flash('Mot de passe mis à jour.', 'success')
    else:
        for field_errors in pw_form.errors.values():
            for error in field_errors:
                flash(error, 'danger')
    return redirect(url_for('settings.index'))


@settings_bp.route('/invite', methods=['POST'])
@login_required
def generate_invite():
    household = current_user.household
    household.generate_invite()
    db.session.commit()
    flash('Lien d\'invitation généré (valable 48h).', 'success')
    return redirect(url_for('settings.index'))
