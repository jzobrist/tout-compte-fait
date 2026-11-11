from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, abort
from flask_login import login_user, logout_user, login_required, current_user
from urllib.parse import urlsplit
from app.extensions import db
from app.models import User, Household
from app.routes.forms import RegisterForm, LoginForm, JoinHouseholdForm
from app.services.household_service import create_household, create_personal_pot

auth_bp = Blueprint('auth', __name__)

def registration_open():
    """Public sign-up allowed by config, or no account exists yet (first account of a fresh instance)."""
    return bool(current_app.config.get('ALLOW_REGISTRATION')) or User.query.first() is None


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if not registration_open():
        abort(404)
    form = RegisterForm()
    if form.validate_on_submit():
        if User.query.filter_by(email=form.email.data).first():
            flash('Email déjà utilisé.', 'danger')
            return render_template('auth/register.html', form=form)
        user = User(
            email=form.email.data,
            display_name=form.display_name.data,
        )
        user.set_password(form.password.data)
        create_household(form.household_name.data, user)
        login_user(user)
        return redirect(url_for('dashboard.index'))
    return render_template('auth/register.html', form=form)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            next_page = request.args.get('next')
            if not next_page or urlsplit(next_page).netloc != '':
                next_page = url_for('dashboard.index')
            return redirect(next_page)
        flash('Email ou mot de passe invalide.', 'danger')
    return render_template('auth/login.html', form=form, registration_open=registration_open())

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))

@auth_bp.route('/invite/<token>', methods=['GET', 'POST'])
def join(token):
    household = Household.query.filter_by(invite_code=token).first_or_404()
    if not household.is_invite_valid():
        flash("Ce lien d'invitation a expiré.", 'danger')
        return redirect(url_for('auth.login'))
    form = JoinHouseholdForm()
    if form.validate_on_submit():
        if User.query.filter_by(email=form.email.data).first():
            flash('Email déjà utilisé.', 'danger')
            return render_template('auth/invite.html', form=form, household=household)
        user = User(
            email=form.email.data,
            display_name=form.display_name.data,
            household_id=household.id,
        )
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.flush()
        create_personal_pot(user, commit=False)
        household.invalidate_invite()
        db.session.commit()
        login_user(user)
        return redirect(url_for('dashboard.index'))
    return render_template('auth/invite.html', form=form, household=household)

@auth_bp.route('/settings/invite', methods=['POST'])
@login_required
def generate_invite():
    household = current_user.household
    household.generate_invite()
    db.session.commit()
    invite_url = url_for('auth.join', token=household.invite_code, _external=True)
    flash(f"Lien d'invitation (valable 48h) : {invite_url}", 'info')
    return redirect(url_for('dashboard.index'))
