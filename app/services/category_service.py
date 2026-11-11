from datetime import datetime, timezone

from sqlalchemy import func, or_

from app.extensions import db
from app.models import AlertLog, BudgetAdjustment, Category, CategoryNotice, Operation, Pot
from app.services.budget_service import get_or_create_budget_month, recalculate_budget_month
from app.services.access_service import can_access_pot


class CategoryError(Exception):
    """A category operation refused for a user-facing reason."""

_SETTING_KEYS = ('amount', 'percentage', 'min_amount', 'max_amount', 'carry_over')


def _ensure_family(category):
    """Give a row with a NULL family_id (inserted outside the ORM hook) its own family before others join it."""
    if category.family_id is None and category.id is not None:
        category.family_id = category.id


def family_key(category):
    """Grouping key of the family; a NULL family_id is a single-row family, never shared with other NULL rows."""
    return category.family_id if category.family_id is not None else ('solo', category.id)


def new_exemplar(source, pot, scope):
    """Add (without committing) a default-settings copy of `source` in `pot`, in the same family."""
    _ensure_family(source)
    exemplar = Category(
        pot_id=pot.id,
        family_id=source.family_id,
        name=source.name,
        type=source.type,
        role=source.role,
        scope=scope,
        amount=None,
        percentage=None,
        min_amount=None,
        max_amount=None,
        carry_over=source.type != 'fixed_auto',
        constrained=False,
        provision=False,
    )
    db.session.add(exemplar)
    return exemplar


def _absorb(existing, source):
    """Make an existing same-name category join the family of `source`, clearing settings its type lacks."""
    _ensure_family(source)
    existing.family_id = source.family_id
    existing.scope = 'shared'
    if existing.type != source.type:
        existing.type = source.type
        existing.amount = None
        existing.percentage = None
        existing.min_amount = None
        existing.max_amount = None
        existing.auto_day = None
        if source.type == 'fixed_auto':
            existing.carry_over = False


def attach_shared_families(pot):
    """Give a new personal pot one exemplar of every shared family of its household not yet present."""
    shared = (Category.query.join(Pot, Category.pot_id == Pot.id)
              .filter(Pot.household_id == pot.household_id, Pot.kind == 'personal',
                      Pot.id != pot.id, Category.scope == 'shared')
              .order_by(Category.id).all())
    families = {}
    for cat in shared:
        families.setdefault(family_key(cat), cat)
    present = Category.query.filter_by(pot_id=pot.id).all()
    held = {family_key(c) for c in present}
    for family_id, source in families.items():
        if family_id not in held:
            _join_shared(source, pot, present)
    db.session.flush()


def _join_shared(source, pot, present=None):
    """Put `pot` in the shared family of `source`: absorb a same-name category, else add an exemplar."""
    if present is None:
        present = Category.query.filter_by(pot_id=pot.id).all()
    existing = next((c for c in present if c.name.lower() == source.name.lower()), None)
    if existing is not None:
        _absorb(existing, source)
        return existing
    return new_exemplar(source, pot, 'shared')


def family_members(category):
    """Every exemplar of the family, whatever the pot (internal use only)."""
    if category.family_id is None:
        return [category]
    return Category.query.filter_by(family_id=category.family_id).order_by(Category.id).all()


def display_name(category):
    return category.name + (' (Commun)' if category.pot.is_common else ' (Perso)')


def _actionable_members(user, category):
    """(members `user` may act on, other members' hidden 'own' exemplars). Nothing is written.

    A family operation never modifies nor deletes an 'own' exemplar in a pot the actor cannot access:
    the caller splits those off with `_detach` before its first write.
    """
    if not can_access_pot(user, category.pot):
        raise LookupError('category')
    members = family_members(category)
    hidden = [m for m in members if m.scope == 'own' and not can_access_pot(user, m.pot)]
    return [m for m in members if m not in hidden], hidden


def _detach(hidden):
    """Silently move each exemplar into its own family (no notice: nothing changes for its owner)."""
    for member in hidden:
        member.family_id = member.id
    if hidden:
        db.session.flush()


def _is_locked(members):
    return any(m.role == 'common_transfer' for m in members)


def check_name_free(user, name, pots, exclude=None):
    """Refuse a name already taken in a pot `user` can access; return the clashing categories elsewhere.

    `exclude` is a category whose family is left out of the check.
    """
    query = Category.query.filter(Category.pot_id.in_([p.id for p in pots]),
                                  func.lower(Category.name) == name.strip().lower())
    if exclude is not None:
        query = query.filter(Category.id.notin_([m.id for m in family_members(exclude)]))
    to_absorb = []
    for cat in query.all():
        if can_access_pot(user, cat.pot):
            raise CategoryError('Une catégorie porte déjà ce nom.')
        to_absorb.append(cat)
    return to_absorb


def _apply_settings(cat, settings):
    pot = cat.pot or db.session.get(Pot, cat.pot_id)
    for key in _SETTING_KEYS:
        if key in settings:
            setattr(cat, key, settings[key])
    cat.constrained = (bool(settings.get('constrained')) and not pot.is_common
                       and cat.type in ('fixed_auto', 'fixed_amount'))
    cat.provision = bool(settings.get('provision')) and pot.is_common


def create_category(user, name, type, common, personal, settings):
    """Create a category in the chosen places; shared personal ones reach every member's personal pot."""
    name = name.strip()
    if not common and personal is None:
        raise CategoryError('Choisissez au moins un emplacement.')
    pots = Pot.query.filter_by(household_id=user.household_id).all()
    common_pot = next(p for p in pots if p.kind == 'common')
    own_pot = next(p for p in pots if p.kind == 'personal' and p.owner_id == user.id)
    targets = ([common_pot] if common else []) + ([own_pot] if personal else [])
    check_name_free(user, name, targets)

    created = []
    for pot in targets:
        scope = None if pot.is_common else personal
        if created:
            cat = new_exemplar(created[0], pot, scope)
        else:
            cat = Category(pot_id=pot.id, name=name, type=type, scope=scope,
                           carry_over=type != 'fixed_auto')
            db.session.add(cat)
        cat.pot = pot
        _apply_settings(cat, settings.get('common' if pot.is_common else 'personal', {}))
        db.session.flush()
        created.append(cat)
    if personal == 'shared':
        for pot in pots:
            if pot.kind == 'personal' and pot.id != own_pot.id:
                created.append(_join_shared(created[-1], pot))
    db.session.commit()
    return created


def update_settings(user, category, settings, commit=True):
    """Apply amount/percentage/… settings to one exemplar `user` can access."""
    if not can_access_pot(user, category.pot):
        raise LookupError('category')
    _apply_settings(category, settings)
    if commit:
        db.session.commit()


def rename_family(user, category, name, commit=True):
    members, hidden = _actionable_members(user, category)
    if _is_locked(members):
        raise CategoryError('Cette catégorie ne peut pas être modifiée.')
    name = name.strip()
    pots = [p for p in {m.pot for m in members} if can_access_pot(user, p)]
    check_name_free(user, name, pots, exclude=category)
    _detach(hidden)
    for member in members:
        member.name = name
    if commit:
        db.session.commit()


def families_for(user):
    """One entry per family with an exemplar accessible to `user`."""
    cats = (Category.query.join(Pot, Category.pot_id == Pot.id)
            .filter(Pot.household_id == user.household_id).order_by(Category.id).all())
    locked = {family_key(c) for c in cats if c.role == 'common_transfer'}
    families = {}
    for cat in cats:
        if not can_access_pot(user, cat.pot):
            continue
        fam = families.setdefault(family_key(cat), {
            'family_id': cat.family_id, 'name': cat.name, 'type': cat.type,
            'exemplars': [], 'locked': family_key(cat) in locked})
        fam['exemplars'].append(cat)
    for fam in families.values():
        exemplars = fam['exemplars']
        flags = {'Commun': any(c.pot.is_common for c in exemplars),
                 'Perso': any(not c.pot.is_common and c.scope == 'shared' for c in exemplars),
                 'Moi': any(not c.pot.is_common and c.scope == 'own' for c in exemplars)}
        fam['badges'] = [b for b in ('Commun', 'Perso', 'Moi') if flags[b]]
    return sorted(families.values(), key=lambda f: f['name'].lower())


def _scope_targets(user, category, common, personal):
    """Validate a scope change; return (actionable members, hidden 'own' exemplars to detach)."""
    members, hidden = _actionable_members(user, category)
    if _is_locked(members):
        raise CategoryError('Cette catégorie ne peut pas être modifiée.')
    if not common and personal is None:
        raise CategoryError('Choisissez au moins un emplacement.')
    return members, hidden


def _exemplars_to_remove(user, members, common, personal):
    removed = []
    for member in members:
        if member.pot.is_common:
            if not common:
                removed.append(member)
        elif personal is None:
            removed.append(member)
        elif personal == 'own' and member.pot.owner_id != user.id:
            removed.append(member)
    return removed


def scope_change_impact(user, category, common, personal):
    """Like deletion_impact, for the exemplars a scope change would remove (accessible pots only)."""
    members, _ = _scope_targets(user, category, common, personal)
    to_reclassify = pending_deleted = 0
    for member in _exemplars_to_remove(user, members, common, personal):
        if not can_access_pot(user, member.pot):
            continue
        bank, pending = _split_operations(member)
        to_reclassify += len(bank)
        pending_deleted += len(pending)
    return {'to_reclassify': to_reclassify, 'pending_deleted': pending_deleted}


def _add_scope_exemplars(user, category, members, common, personal):
    """Add (without committing) the exemplars the new scope needs; return those it removes."""
    pots = Pot.query.filter_by(household_id=user.household_id).all()
    common_pot = next(p for p in pots if p.kind == 'common')
    own_pot = next(p for p in pots if p.kind == 'personal' and p.owner_id == user.id)
    by_pot = {m.pot_id: m for m in members}
    removed = _exemplars_to_remove(user, members, common, personal)

    if common and common_pot.id not in by_pot:
        check_name_free(user, category.name, [common_pot], exclude=category)
        new_exemplar(category, common_pot, None)
    if personal is not None:
        mine = by_pot.get(own_pot.id)
        if mine is None:
            check_name_free(user, category.name, [own_pot], exclude=category)
            mine = new_exemplar(category, own_pot, personal)
        mine.scope = personal
        db.session.flush()
        if personal == 'shared':
            for pot in pots:
                if pot.kind == 'personal' and pot.id != own_pot.id and pot.id not in by_pot:
                    _join_shared(mine, pot)
    db.session.flush()
    return removed


def change_scope(user, category, common, personal):
    """Add or remove the family's exemplars so it lives exactly in the chosen places."""
    members, hidden = _scope_targets(user, category, common, personal)
    _detach(hidden)
    removed = _add_scope_exemplars(user, category, members, common, personal)
    if removed:
        delete_exemplars(user, removed)
    else:
        db.session.commit()


def scope_impacts(user, category):
    """Impact of every reachable scope choice, keyed "<common 0/1>|<personal ''/shared/own>"."""
    impacts = {}
    for common in (True, False):
        for personal in ('', 'shared', 'own'):
            try:
                impacts[f"{int(common)}|{personal}"] = scope_change_impact(
                    user, category, common, personal or None)
            except CategoryError:
                continue
    return impacts


def relevant_settings(settings, type):
    """Drop the settings the type cannot carry (hidden form fields still get posted)."""
    keys = ('percentage', 'min_amount', 'max_amount') if type != 'dynamic' else ('amount',)
    return {**settings, **{k: None for k in keys}}


def _apply_family_settings(user, category, type, settings, skip=()):
    for member in family_members(category):
        if member.id in skip or not can_access_pot(user, member.pot):
            continue
        values = settings.get('common' if member.pot.is_common else 'personal')
        if values is not None:
            update_settings(user, member, relevant_settings(values, type), commit=False)


def update_family(user, category, name, type, common, personal, settings):
    """Rename, change scope and type, and set each accessible exemplar's settings in one transaction.

    `settings` is {'common': {...}, 'personal': {...}}. Every check runs before the first write.
    A locked family only takes settings: any name, type or scope change is refused.
    """
    members, hidden = _actionable_members(user, category)
    name = name.strip()
    pots = Pot.query.filter_by(household_id=user.household_id).all()
    own_pot = next(p for p in pots if p.kind == 'personal' and p.owner_id == user.id)
    common_pot = next(p for p in pots if p.kind == 'common')
    if _is_locked(members):
        mine = next((m for m in members if m.pot_id == own_pot.id), None)
        current = (any(m.pot.is_common for m in members), mine.scope if mine else None)
        if (name, type, (common, personal)) != (category.name, category.type, current):
            raise CategoryError('Cette catégorie ne peut pas être modifiée.')
        try:
            _apply_family_settings(user, category, type, settings)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return
    if not common and personal is None:
        raise CategoryError('Choisissez au moins un emplacement.')
    if type not in ('fixed_auto', 'fixed_amount', 'dynamic'):
        raise CategoryError('Type de catégorie invalide.')
    held = {m.pot_id for m in members}
    wanted = ([common_pot] if common else []) + ([own_pot] if personal is not None else [])
    check_pots = {m.pot_id: m.pot for m in members if can_access_pot(user, m.pot)}
    check_pots.update({p.id: p for p in wanted if p.id not in held})
    check_name_free(user, name, list(check_pots.values()), exclude=category)

    try:
        _detach(hidden)
        if name != category.name:
            rename_family(user, category, name, commit=False)
        removed = _add_scope_exemplars(user, category, members, common, personal)
        change_type(user, category, type, commit=False)
        _apply_family_settings(user, category, type, settings, skip={m.id for m in removed})
        db.session.flush()
        if removed:
            delete_exemplars(user, removed)
        else:
            db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def change_type(user, category, type, commit=True):
    """Apply a new type to every exemplar, clearing the settings it cannot carry."""
    members, hidden = _actionable_members(user, category)
    if _is_locked(members):
        raise CategoryError('Cette catégorie ne peut pas être modifiée.')
    if type not in ('fixed_auto', 'fixed_amount', 'dynamic'):
        raise CategoryError('Type de catégorie invalide.')
    _detach(hidden)
    for member in members:
        member.type = type
        if type == 'dynamic':
            member.amount = None
            member.constrained = False
        else:
            member.percentage = None
            member.min_amount = None
            member.max_amount = None
        if type != 'fixed_auto':
            member.auto_day = None
    if commit:
        db.session.commit()


def _split_operations(category):
    """(bank movements, pending manual entries) booked on `category`."""
    ops = Operation.query.filter_by(category_id=category.id).all()
    return ([o for o in ops if o.bank_reference is not None],
            [o for o in ops if o.bank_reference is None])


def deletion_impact(user, category):
    """Counts over the exemplars of the family in pots `user` can access."""
    to_reclassify = pending_deleted = 0
    for member in family_members(category):
        if not can_access_pot(user, member.pot):
            continue
        bank, pending = _split_operations(member)
        to_reclassify += len(bank)
        pending_deleted += len(pending)
    return {'to_reclassify': to_reclassify, 'pending_deleted': pending_deleted}


def _notice_message(name, actor, to_reclassify, pending_deleted):
    parts = []
    if to_reclassify:
        parts.append(f"{to_reclassify} opération{'s' if to_reclassify > 1 else ''} à reclasser")
    if pending_deleted:
        parts.append(f"{pending_deleted} saisie{'s' if pending_deleted > 1 else ''} "
                     f"en attente supprimée{'s' if pending_deleted > 1 else ''}")
    return f"La catégorie {name} a été supprimée par {actor.display_name} — {', '.join(parts)}."


def delete_exemplars(actor, exemplars):
    """Delete categories without losing bank data: movements return to their account's pot, unclassified."""
    touched = set()
    notices = []
    for cat in exemplars:
        bank, pending = _split_operations(cat)
        for op in bank:
            touched.add((op.pot_id, op.date.year, op.date.month))
            op.classification = None
            op.category_id = None
            op.contributor_id = None
            op.entered_amount = None
            if op.bank_account is not None:
                op.pot_id = op.bank_account.pot_id
            touched.add((op.pot_id, op.date.year, op.date.month))
        for op in pending:
            touched.add((op.pot_id, op.date.year, op.date.month))
            db.session.delete(op)
        BudgetAdjustment.query.filter(or_(BudgetAdjustment.from_category_id == cat.id,
                                          BudgetAdjustment.to_category_id == cat.id)
                                      ).delete(synchronize_session=False)
        AlertLog.query.filter_by(category_id=cat.id).update({'category_id': None})
        pot = cat.pot
        if not pot.is_common and pot.owner_id != actor.id and (bank or pending):
            notices.append(CategoryNotice(
                user_id=pot.owner_id,
                message=_notice_message(cat.name, actor, len(bank), len(pending))))
        db.session.flush()
        db.session.delete(cat)
    db.session.add_all(notices)
    db.session.flush()
    db.session.expire_all()
    for pot_id, year, month in sorted(touched):
        recalculate_budget_month(get_or_create_budget_month(pot_id, year, month))
    db.session.commit()


def delete_family(user, category):
    members, hidden = _actionable_members(user, category)
    if _is_locked(members):
        raise CategoryError('Cette catégorie ne peut pas être supprimée.')
    _detach(hidden)
    delete_exemplars(user, members)


def pending_notices(user):
    return (CategoryNotice.query.filter_by(user_id=user.id, seen_at=None)
            .order_by(CategoryNotice.id).all())


def mark_seen(notices):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for notice in notices:
        notice.seen_at = now
    db.session.commit()
