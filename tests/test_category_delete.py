from datetime import date

import pytest

from app.models import (AlertLog, BankAccount, BudgetAdjustment, BudgetMonth, Category,
                        CategoryNotice, Operation)
from app.services import category_service, pointage_service
from app.services.category_service import CategoryError


def _cat(db, pot, name='Sport', **kw):
    cat = Category(name=name, type='fixed_amount', amount=50, pot_id=pot.id, **kw)
    db.session.add(cat)
    db.session.commit()
    return cat


def _account(db, pot):
    acc = BankAccount(pot_id=pot.id, name='Compte', bank_name='Banque')
    db.session.add(acc)
    db.session.commit()
    return acc


def _bank_op(db, user, cat, acc, ref, direction='debit', pot=None):
    op = Operation(pot_id=(pot or cat.pot).id, user_id=user.id, amount=10.0, date=date(2026, 11, 3),
                   label='x', direction=direction, bank_account_id=acc.id, bank_reference=ref,
                   bank_label='x', bank_date=date(2026, 11, 3), category_id=cat.id,
                   classification='category' if direction == 'debit' else 'refund')
    db.session.add(op)
    db.session.commit()
    return op


def _pending(db, user, cat):
    op = Operation(pot_id=cat.pot_id, user_id=user.id, amount=5.0, date=date(2026, 11, 4),
                   label='p', category_id=cat.id, classification='category')
    db.session.add(op)
    db.session.commit()
    return op


def _shared(db, h, name='Sport'):
    a = _cat(db, h.personal_a, name, scope='shared')
    b = _cat(db, h.personal_b, name, scope='shared', family_id=a.family_id)
    return a, b


def test_delete_keeps_bank_movements_unclassified(db, household_ab):
    h = household_ab
    cat = _cat(db, h.personal_a, scope='own')
    acc = _account(db, h.personal_a)
    debit = _bank_op(db, h.user_a, cat, acc, 'R1')
    refund = _bank_op(db, h.user_a, cat, acc, 'R2', direction='credit')
    pend = _pending(db, h.user_a, cat)
    debit.entered_amount = 12.0  # a reconciliation: « Délier » must not come back after the delete
    db.session.commit()
    ids = (debit.id, refund.id, pend.id)
    category_service.delete_family(h.user_a, cat)
    assert db.session.get(Operation, ids[2]) is None
    for op_id in ids[:2]:
        op = db.session.get(Operation, op_id)
        assert op.classification is None and op.category_id is None
        assert op.entered_amount is None


def test_delete_returns_movement_to_account_pot(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.personal_a)
    op = Operation(pot_id=h.personal_a.id, user_id=h.user_a.id, amount=40.0, date=date(2026, 11, 10),
                   label='CB', direction='debit', bank_account_id=acc.id, bank_reference='R9',
                   bank_label='CB', bank_date=date(2026, 11, 10))
    db.session.add(op)
    db.session.commit()
    pointage_service.classify(op, h.user_a, 'category', category=cat)
    assert op.pot_id == h.common.id
    category_service.delete_family(h.user_a, cat)
    op = db.session.get(Operation, op.id)
    assert op.pot_id == h.personal_a.id
    panel = pointage_service.build_panel(h.personal_a, 'a-classer', h.user_a)
    assert [r['op'].id for r in panel['rows']] == [op.id]


def test_delete_with_adjustments_and_alerts(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    other = _cat(db, h.common, 'Autre')
    bm = BudgetMonth(pot_id=h.common.id, year=2026, month=11)
    db.session.add(bm)
    db.session.commit()
    db.session.add_all([
        BudgetAdjustment(budget_month_id=bm.id, user_id=h.user_a.id, from_category_id=cat.id,
                         to_category_id=other.id, amount=5),
        BudgetAdjustment(budget_month_id=bm.id, user_id=h.user_a.id, from_category_id=other.id,
                         to_category_id=cat.id, amount=3),
        AlertLog(pot_id=h.common.id, category_id=cat.id, alert_type='overspending')])
    db.session.commit()
    category_service.delete_family(h.user_a, cat)
    assert BudgetAdjustment.query.count() == 0
    assert AlertLog.query.one().category_id is None
    assert db.session.get(Category, cat.id) is None


def test_deletion_impact_counts_only_accessible(db, household_ab):
    h = household_ab
    a, b = _shared(db, h)
    acc_a, acc_b = _account(db, h.personal_a), _account(db, h.personal_b)
    for i in range(2):
        _bank_op(db, h.user_a, a, acc_a, f'A{i}')
    for i in range(3):
        _bank_op(db, h.user_b, b, acc_b, f'B{i}')
    assert category_service.deletion_impact(h.user_a, a) == {'to_reclassify': 2, 'pending_deleted': 0}


def test_partner_gets_notice(db, household_ab):
    h = household_ab
    a, b = _shared(db, h)
    acc_b = _account(db, h.personal_b)
    for i in range(3):
        _bank_op(db, h.user_b, b, acc_b, f'B{i}')
    _pending(db, h.user_b, b)
    category_service.delete_family(h.user_a, a)
    notice = CategoryNotice.query.one()
    assert notice.user_id == h.user_b.id
    assert notice.message == ('La catégorie Sport a été supprimée par A — '
                              '3 opérations à reclasser, 1 saisie en attente supprimée.')
    assert category_service.pending_notices(h.user_a) == []
    category_service.mark_seen(category_service.pending_notices(h.user_b))
    assert category_service.pending_notices(h.user_b) == []


def test_singular_notice_and_no_notice_when_empty(db, household_ab):
    h = household_ab
    a, b = _shared(db, h)
    acc_b = _account(db, h.personal_b)
    _bank_op(db, h.user_b, b, acc_b, 'B1')
    category_service.delete_family(h.user_a, a)
    assert CategoryNotice.query.one().message.endswith('— 1 opération à reclasser.')
    a, b = _shared(db, h, 'Vide')
    category_service.delete_family(h.user_a, a)
    assert CategoryNotice.query.count() == 1


def test_delete_locked_family_refused(db, household_ab):
    h = household_ab
    locked = Category.query.filter_by(role='common_transfer').first()
    assert locked is not None
    with pytest.raises(CategoryError):
        category_service.delete_family(h.user_a if locked.pot.owner_id == h.user_a.id else h.user_b, locked)
    assert db.session.get(Category, locked.id) is not None


def test_delete_inaccessible_is_lookup_error(db, household_ab):
    h = household_ab
    cat = _cat(db, h.personal_b, scope='own')
    with pytest.raises(LookupError):
        category_service.delete_family(h.user_a, cat)
