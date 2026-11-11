from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import current_user, login_required
from app.extensions import db
from app.models import BankAccount
from app.models.operation import Operation
from app.routes.forms import BankAccountForm
from app.services import account_service
from app.services.access_service import accessible_pots, get_current_pot, pot_from_form_or_404, load_pot_object_or_404

accounts_bp = Blueprint('accounts', __name__, url_prefix='/accounts')


@accounts_bp.route('', methods=['GET', 'POST'])
@login_required
def index():
    form = BankAccountForm()
    pot = get_current_pot()
    if form.validate_on_submit():
        pot = pot_from_form_or_404(form.pot_id.data)
        db.session.add(BankAccount(
            pot_id=pot.id,
            name=form.name.data,
            bank_name=form.bank_name.data,
            account_number_hint=form.account_number_hint.data or None,
            overdraft_limit=form.overdraft_limit.data or 0.0,
        ))
        db.session.commit()
        return redirect(url_for('accounts.index'))
    accounts = BankAccount.query.filter_by(pot_id=pot.id).order_by(BankAccount.name).all()
    form.pot_id.data = pot.id
    return render_template(
        'accounts/index.html', form=form,
        active=[a for a in accounts if not a.archived],
        archived=[a for a in accounts if a.archived],
    )


@accounts_bp.route('/<int:account_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(account_id):
    account = load_pot_object_or_404(BankAccount, account_id)
    form = BankAccountForm(obj=account)
    if form.validate_on_submit():
        account.name = form.name.data
        account.bank_name = form.bank_name.data
        account.account_number_hint = form.account_number_hint.data or None
        account.overdraft_limit = form.overdraft_limit.data or 0.0
        if form.pot_id.data and str(account.pot_id) != str(form.pot_id.data):
            target = pot_from_form_or_404(form.pot_id.data)
            try:
                account_service.move_account(current_user, account, target)
            except account_service.ForeignLinkError:
                db.session.rollback()
                flash('Ce compte est aussi relié par un autre membre : il doit rester dans le pot commun.',
                      'danger')
                return redirect(url_for('accounts.index'))
        db.session.commit()
        return redirect(url_for('accounts.index'))
    form.pot_id.data = account.pot_id
    return render_template('accounts/edit.html', form=form, account=account,
                           pots=accessible_pots(current_user))


@accounts_bp.route('/<int:account_id>/archive', methods=['POST'])
@login_required
def archive(account_id):
    account = load_pot_object_or_404(BankAccount, account_id)
    account.archived = not account.archived
    db.session.commit()
    return redirect(url_for('accounts.index'))


@accounts_bp.route('/<int:account_id>/delete', methods=['POST'])
@login_required
def delete(account_id):
    account = load_pot_object_or_404(BankAccount, account_id)
    if Operation.query.filter_by(bank_account_id=account.id).first():
        flash('Ce compte a des opérations : archivez-le plutôt.', 'danger')
    else:
        db.session.delete(account)
        db.session.commit()
    return redirect(url_for('accounts.index'))
