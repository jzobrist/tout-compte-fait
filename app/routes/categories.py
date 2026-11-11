from flask import Blueprint, render_template, request, redirect, url_for, abort, flash
from flask_login import login_required, current_user
from app.routes.forms import CategoryForm, CategorySettingsForm
from app.services import category_service
from app.services.category_service import CategoryError
from app.services.access_service import load_pot_object_or_404
from app.models import Category

categories_bp = Blueprint('categories', __name__, url_prefix='/categories')

_KINDS = (('common', 'Réglages — Commun'), ('personal', 'Réglages — Perso'))


def _kind(cat):
    return 'common' if cat.pot.is_common else 'personal'


def _settings_forms(exemplars=None):
    """[(kind, label, form)] — one block per exemplar given, or both blocks when creating."""
    by_kind = None if exemplars is None else {_kind(c): c for c in exemplars}
    blocks = []
    for kind, label in _KINDS:
        if by_kind is not None and kind not in by_kind:
            continue
        obj = by_kind[kind] if by_kind else None
        blocks.append((kind, label, CategorySettingsForm(prefix=kind, obj=obj, meta={'csrf': False})))
    return blocks


def _personal_value(personal):
    return personal or None


def _invalid(blocks):
    return not all([sform.validate() for _, _, sform in blocks])


@categories_bp.route('', methods=['GET', 'POST'])
@login_required
def index():
    form = CategoryForm()
    blocks = _settings_forms()
    status = 200
    if form.validate_on_submit():
        if _invalid(blocks):
            status = 400
        else:
            settings = {kind: category_service.relevant_settings(sform.values(), form.type.data) for kind, _, sform in blocks}
            try:
                category_service.create_category(
                    current_user, form.name.data, form.type.data, bool(form.common.data),
                    _personal_value(form.personal.data), settings)
            except CategoryError as exc:
                flash(str(exc), 'danger')
                status = 400
            else:
                return redirect(url_for('categories.index'))
    elif request.method == 'POST':
        status = 400
    families = category_service.families_for(current_user)
    for fam in families:
        fam['impact'] = category_service.deletion_impact(current_user, fam['exemplars'][0])
    return render_template('categories/index.html', families=families, form=form,
                           settings_forms=blocks, cat=None), status


def _current_scope(exemplars):
    common = any(c.pot.is_common for c in exemplars)
    mine = next((c for c in exemplars if not c.pot.is_common), None)
    return common, (mine.scope if mine else None)


@categories_bp.route('/<int:cat_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(cat_id):
    cat = load_pot_object_or_404(Category, cat_id)
    fam = next((f for f in category_service.families_for(current_user)
                if any(c.id == cat.id for c in f['exemplars'])), None)
    if fam is None:
        abort(404)
    exemplars = fam['exemplars']
    common_now, personal_now = _current_scope(exemplars)
    form = CategoryForm(obj=cat)
    blocks = _settings_forms(exemplars)
    status = 200
    if request.method == 'GET':
        form.common.data = common_now
        form.personal.data = personal_now or ''
    elif form.validate_on_submit() and not _invalid(blocks):
        try:
            category_service.update_family(
                current_user, cat, form.name.data, form.type.data, bool(form.common.data),
                _personal_value(form.personal.data),
                {kind: sform.values() for kind, _, sform in blocks})
        except LookupError:
            abort(404)
        except CategoryError as exc:
            flash(str(exc), 'danger')
            status = 400
        else:
            return redirect(url_for('categories.index'))
    else:
        status = 400
    template = 'categories/_form.html' if request.headers.get('HX-Request') else 'categories/edit.html'
    return render_template(template, form=form, cat=cat, settings_forms=blocks,
                           locked=fam['locked'], impacts=category_service.scope_impacts(current_user, cat)), status


@categories_bp.route('/<int:cat_id>/delete', methods=['POST'])
@login_required
def delete(cat_id):
    cat = load_pot_object_or_404(Category, cat_id)
    try:
        category_service.delete_family(current_user, cat)
    except LookupError:
        abort(404)
    except CategoryError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('categories.index'))
