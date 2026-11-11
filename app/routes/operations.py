import calendar
import datetime

from flask import Blueprint, render_template, request, redirect, url_for, abort, flash, session
from flask_login import login_required, current_user
from app.extensions import db
from app.models import BankAccount, Category, User
from app.models.operation import Operation
from app.routes.forms import OperationForm
from app.services.import_service import import_banque_populaire_csv
from app.services.access_service import accessible_pots, can_access_pot, get_current_pot, load_pot_object_or_404
from app.routes.dashboard import detail_response_or_none
from app.services.budget_service import get_or_create_budget_month, recalculate_budget_month
from app.services.pointage_service import ClassificationError, build_panel, classify, recategorize_entry
from app.services.reconcile_service import eligible_entries, link, unlink

operations_bp = Blueprint('operations', __name__, url_prefix='/operations')

@operations_bp.route('/form')
@login_required
def form():
    """HTMX: return operation form fragment."""
    f = OperationForm()
    cats = Category.query.filter_by(pot_id=get_current_pot().id).order_by(Category.name).all()
    f.category_id.choices = [(c.id, c.name) for c in cats]
    return render_template('operations/_form.html', form=f)

@operations_bp.route('', methods=['GET', 'POST'])
@login_required
def add():
    f = OperationForm()
    # Validation accepts categories of any pot the user can access (the operation takes the
    # category's pot, never the session pot); rendering only ever offers the current pot's.
    pot_ids = [p.id for p in accessible_pots(current_user)]
    cats = Category.query.filter(Category.pot_id.in_(pot_ids)).order_by(Category.name).all()
    f.category_id.choices = [(c.id, c.name) for c in cats]
    if f.validate_on_submit():
        category = load_pot_object_or_404(Category, f.category_id.data)
        op = Operation(
            pot_id=category.pot_id,
            user_id=current_user.id,
            category_id=category.id,
            amount=f.amount.data,
            date=f.date.data,
            label=f.label.data,
            direction='debit',
            classification='category',
            entered_amount=f.amount.data,
        )
        db.session.add(op)
        db.session.commit()
        if request.headers.get('HX-Request'):
            from app.services.budget_service import (
                get_or_create_budget_month, recalculate_budget_month,
                get_spent_for_category, compute_daily_metrics
            )
            op_date = op.date
            bm = get_or_create_budget_month(category.pot_id, op_date.year, op_date.month)
            recalculate_budget_month(bm)
            all_cats = Category.query.filter_by(pot_id=category.pot_id).all()
            today = datetime.date.today()
            days_in_month = calendar.monthrange(op_date.year, op_date.month)[1]
            current_day = today.day if (op_date.year == today.year and op_date.month == today.month) else days_in_month
            cat_data = []
            for cat in all_cats:
                spent = get_spent_for_category(bm, cat)
                metrics = compute_daily_metrics(bm, cat, current_day, days_in_month, spent)
                cat_data.append({'category': cat, 'metrics': metrics, 'type': cat.type})
            return render_template('dashboard/_category_list.html', cat_data=cat_data, year=bm.year, month=bm.month)
        return redirect(url_for('dashboard.index'))
    # Validation failed / GET: never re-render choices wider than the current pot
    cats = Category.query.filter_by(pot_id=get_current_pot().id).order_by(Category.name).all()
    f.category_id.choices = [(c.id, c.name) for c in cats]
    if request.headers.get('HX-Request'):
        return render_template('operations/_form.html', form=f), 422
    return render_template('operations/list.html', form=f)

@operations_bp.route('/<int:op_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(op_id):
    op = load_pot_object_or_404(Operation, op_id)
    if op.is_bank_movement and (op.direction == 'credit' or op.classification != 'category'):
        abort(400)
    cats = Category.query.filter_by(pot_id=op.pot_id).order_by(Category.name).all()
    f = OperationForm(obj=op)
    f.category_id.choices = [(c.id, c.name) for c in cats]
    if f.validate_on_submit():
        was_pending = op.is_pending
        if not op.is_bank_movement:
            op.amount = f.amount.data
        op.category_id = f.category_id.data
        if op.category_id:
            op.classification = 'category'
        if was_pending:  # pending entry: not a bank movement, so the amount was just updated
            op.entered_amount = f.amount.data
        op.label = f.label.data
        op.date = f.date.data
        db.session.commit()
        return redirect(url_for('history.index'))
    return render_template('operations/edit.html', form=f, op=op)


@operations_bp.route('/<int:op_id>/delete', methods=['POST'])
@login_required
def delete(op_id):
    op = load_pot_object_or_404(Operation, op_id)
    if op.is_bank_movement:
        abort(400)
    pot_id, on = op.pot_id, op.date
    db.session.delete(op)
    db.session.commit()
    if request.form.get('detail_cat'):
        recalculate_budget_month(get_or_create_budget_month(pot_id, on.year, on.month))
        return detail_response_or_none()
    if request.headers.get('HX-Request') and request.form.get('tab'):
        return _panel(request.form['tab'])
    return redirect(request.form.get('next') or url_for('dashboard.index'))


@operations_bp.route('/import', methods=['GET', 'POST'])
@login_required
def import_csv():
    if request.method == 'POST':
        f = request.files.get('csv_file')
        if not f or not f.filename:
            flash('Aucun fichier sélectionné.', 'danger')
            return redirect(url_for('operations.import_csv'))

        raw = f.read()
        for encoding in ('utf-8-sig', 'latin-1'):
            try:
                content = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            flash('Impossible de lire le fichier (encodage non supporté).', 'danger')
            return redirect(url_for('operations.import_csv'))

        account_id = request.form.get('account_id', type=int)
        if account_id is None:
            flash('Choisissez un compte.', 'danger')
            return redirect(url_for('operations.import_csv'))
        account = load_pot_object_or_404(BankAccount, account_id)
        if account.archived:
            flash('Ce compte est archivé : import impossible.', 'danger')
            return redirect(url_for('operations.import_csv'))
        result = import_banque_populaire_csv(content, account, current_user)
        msg = (f'{result.imported} importée(s) — {result.auto_linked} rapprochée(s) automatiquement'
               f' — {result.to_classify} à classer')
        if result.skipped_dup:
            msg += f' — {result.skipped_dup} doublon(s) ignoré(s)'
        flash(msg, 'success' if result.imported else 'warning')
        session['current_pot_id'] = account.pot_id
        return redirect(url_for('operations.pointage'))

    accounts = (BankAccount.query.filter_by(pot_id=get_current_pot().id, archived=False)
                .order_by(BankAccount.name).all())
    return render_template('operations/import.html', accounts=accounts)


def _panel(tab):
    """Render the tab panel (tabs with badges + the active list) for the current pot."""
    return render_template('operations/_pointage_panel.html',
                           **build_panel(get_current_pot(), tab, current_user))


@operations_bp.route('/pointage')
@login_required
def pointage():
    tab = request.args.get('tab', 'a-classer')
    if request.headers.get('HX-Request'):
        return _panel(tab)
    return render_template('operations/pointage.html', panel=_panel(tab))


def _tab_param(default):
    return request.form.get('tab', default)


@operations_bp.route('/<int:op_id>/classify', methods=['POST'])
@login_required
def classify_op(op_id):
    op = load_pot_object_or_404(Operation, op_id)
    if not op.is_bank_movement:
        abort(400)
    category = None
    category_id = request.form.get('category_id', type=int)
    if category_id:
        category = load_pot_object_or_404(Category, category_id)
    try:
        contributor = None
        contributor_id = request.form.get('contributor_id', type=int)
        if contributor_id:
            contributor = db.session.get(User, contributor_id)
        classify(op, current_user, request.form.get('classification', ''), category, contributor)
    except ClassificationError:
        abort(400)
    detail = detail_response_or_none()
    if detail is not None:
        return detail
    if request.headers.get('HX-Request'):
        return _panel(_tab_param('a-classer'))
    return redirect(url_for('operations.pointage', tab=_tab_param('a-classer')))


@operations_bp.route('/<int:op_id>/recategorize', methods=['POST'])
@login_required
def recategorize_op(op_id):
    """Change the category of a manual entry (detail window of a gauge)."""
    op = load_pot_object_or_404(Operation, op_id)
    category_id = request.form.get('category_id', type=int)
    category = load_pot_object_or_404(Category, category_id) if category_id else None
    try:
        recategorize_entry(op, category)
    except ClassificationError:
        abort(400)
    detail = detail_response_or_none()
    if detail is not None:
        return detail
    return redirect(url_for('dashboard.index'))


@operations_bp.route('/<int:op_id>/link', methods=['POST'])
@login_required
def link_op(op_id):
    movement = load_pot_object_or_404(Operation, op_id)
    entry_id = request.form.get('entry_id', type=int)
    if entry_id is None:
        abort(400)
    entry = load_pot_object_or_404(Operation, entry_id)
    if not movement.is_to_classify or entry not in eligible_entries(movement, any_amount=True):
        abort(400)
    link(entry, movement)
    if request.headers.get('HX-Request'):
        return _panel(_tab_param('a-classer'))
    return redirect(url_for('operations.pointage', tab=_tab_param('a-classer')))


@operations_bp.route('/<int:op_id>/unlink', methods=['POST'])
@login_required
def unlink_op(op_id):
    entry = load_pot_object_or_404(Operation, op_id)
    if entry.bank_account is not None and not can_access_pot(current_user, entry.bank_account.pot):
        abort(404)
    try:
        unlink(entry)
    except ValueError:
        abort(400)
    if request.headers.get('HX-Request'):
        return _panel(_tab_param('rapproches'))
    return redirect(url_for('operations.pointage', tab=_tab_param('rapproches')))
