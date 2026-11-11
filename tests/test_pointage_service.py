from datetime import date

import pytest

from app.models import BankAccount, BudgetMonth, Category, Income, Operation
from app.services.budget_service import spent_for_category
from app.services.pointage_service import (
    ClassificationError, Suggestion, classify, normalize_label, suggest,
)


def _cat(db, pot, name='Courses'):
    c = Category(name=name, type='fixed_amount', amount=100.0, pot_id=pot.id)
    db.session.add(c)
    db.session.commit()
    return c


def _account(db, pot):
    acc = BankAccount(pot_id=pot.id, name='Compte', bank_name='Banque')
    db.session.add(acc)
    db.session.commit()
    return acc


def _op(db, h, pot, direction='debit', amount=40.0, account=None, label='CB TEST',
        bank_date=date(2026, 11, 10), **kw):
    op = Operation(
        pot_id=pot.id, user_id=h.user_a.id, amount=amount, date=bank_date, label=label,
        direction=direction, bank_account_id=account.id if account else None,
        bank_label=label, bank_date=bank_date, **kw,
    )
    db.session.add(op)
    db.session.commit()
    return op


def _bm(pot_id, y, m):
    return BudgetMonth.query.filter_by(pot_id=pot_id, year=y, month=m).first()


def test_classify_debit_into_own_pot_category(db, household_ab):
    h = household_ab
    cat = _cat(db, h.personal_a)
    op = _op(db, h, h.personal_a, account=_account(db, h.personal_a))
    classify(op, h.user_a, 'category', cat)
    assert (op.classification, op.category_id, op.pot_id) == ('category', cat.id, h.personal_a.id)
    assert spent_for_category(cat, 2026, 11) == 40.0


def test_classify_personal_card_debit_into_common_category_moves_pot(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    op = _op(db, h, h.personal_a, account=_account(db, h.personal_a))
    classify(op, h.user_a, 'category', cat)
    assert op.pot_id == h.common.id
    assert op.category_id == cat.id


def test_classify_common_account_debit_into_personal_category_refused(db, household_ab):
    h = household_ab
    cat = _cat(db, h.personal_a)
    op = _op(db, h, h.common, account=_account(db, h.common))
    with pytest.raises(ClassificationError):
        classify(op, h.user_a, 'category', cat)


def test_manual_entry_without_account_cannot_use_other_pot_category(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    op = _op(db, h, h.personal_a)
    with pytest.raises(ClassificationError):
        classify(op, h.user_a, 'category', cat)


def test_category_requires_category(db, household_ab):
    h = household_ab
    op = _op(db, h, h.personal_a)
    with pytest.raises(ClassificationError):
        classify(op, h.user_a, 'category')
    credit = _op(db, h, h.personal_a, direction='credit')
    with pytest.raises(ClassificationError):
        classify(credit, h.user_a, 'refund')


def test_classify_credit_as_income_creates_income_in_month(db, household_ab):
    h = household_ab
    h.common.income_start_day = 25
    db.session.commit()
    op = _op(db, h, h.common, direction='credit', amount=2000.0,
             account=_account(db, h.common), label='VIR SALAIRE',
             bank_date=date(2026, 11, 27))
    classify(op, h.user_a, 'income')
    assert op.classification == 'income'
    inc = Income.query.one()
    assert inc.operation_id == op.id and op.income == inc
    assert inc.pot_id == h.common.id and inc.date == date(2026, 11, 27)
    assert _bm(h.common.id, 2026, 12).total_income == 2000.0


def test_reclassify_income_removes_income(db, household_ab):
    h = household_ab
    op = _op(db, h, h.common, direction='credit', amount=500.0, account=_account(db, h.common))
    classify(op, h.user_a, 'income')
    assert _bm(h.common.id, 2026, 11).total_income == 500.0
    classify(op, h.user_a, 'off_budget')
    db.session.expire_all()
    assert Income.query.count() == 0
    assert op.classification == 'off_budget' and op.category_id is None
    assert _bm(h.common.id, 2026, 11).total_income == 0.0


def test_refund_from_personal_account_on_common_category(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.personal_a)
    debit = _op(db, h, h.personal_a, account=acc, amount=100.0)
    classify(debit, h.user_a, 'category', cat)
    refund = _op(db, h, h.personal_a, direction='credit', amount=30.0, account=acc)
    classify(refund, h.user_a, 'refund', cat)
    assert refund.pot_id == h.common.id
    assert spent_for_category(cat, 2026, 11) == 70.0


def test_refund_on_debit_refused(db, household_ab):
    h = household_ab
    cat = _cat(db, h.personal_a)
    op = _op(db, h, h.personal_a)
    with pytest.raises(ClassificationError):
        classify(op, h.user_a, 'refund', cat)


def test_income_on_debit_refused(db, household_ab):
    h = household_ab
    op = _op(db, h, h.personal_a)
    with pytest.raises(ClassificationError):
        classify(op, h.user_a, 'income')


def test_internal_clears_category(db, household_ab):
    h = household_ab
    cat = _cat(db, h.personal_a)
    op = _op(db, h, h.personal_a)
    classify(op, h.user_a, 'category', cat)
    classify(op, h.user_a, 'internal')
    assert op.category_id is None and op.classification == 'internal'


def test_unknown_classification_refused(db, household_ab):
    op = _op(db, household_ab, household_ab.personal_a)
    with pytest.raises(ClassificationError):
        classify(op, household_ab.user_a, 'bogus')


def test_normalize_label():
    assert normalize_label('CB NETFLIX 12/11 FR') == 'CB NETFLIX FR'
    assert normalize_label('  cb  Carrefour, 45 ') == 'CB CARREFOUR'


def test_suggest_uses_most_recent_same_label_same_pot(db, household_ab):
    h = household_ab
    c1, c2 = _cat(db, h.personal_a, 'A'), _cat(db, h.personal_a, 'B')
    old = _op(db, h, h.personal_a, label='CB NETFLIX 01/10', bank_date=date(2026, 10, 1))
    classify(old, h.user_a, 'category', c1)
    new = _op(db, h, h.personal_a, label='CB NETFLIX 12/11', bank_date=date(2026, 11, 12))
    classify(new, h.user_a, 'category', c2)
    cur = _op(db, h, h.personal_a, label='CB NETFLIX 12/12', bank_date=date(2026, 12, 12))
    assert suggest(cur) == Suggestion('category', c2.id)


def test_suggest_none_without_history(db, household_ab):
    op = _op(db, household_ab, household_ab.personal_a)
    assert suggest(op) is None


def test_suggest_ignores_other_pots_and_directions(db, household_ab):
    h = household_ab
    cat = _cat(db, h.personal_b)
    other = _op(db, h, h.personal_b, label='CB X')
    classify(other, h.user_b, 'category', cat)
    cr = _op(db, h, h.personal_a, direction='credit', label='CB X')
    classify(cr, h.user_a, 'off_budget')
    cur = _op(db, h, h.personal_a, label='CB X')
    assert suggest(cur) is None


def test_incomes_page_still_records_and_deletes(db, household_ab):
    h = household_ab
    h.client_a.post('/incomes', data={'amount': 300, 'label': 'Prime', 'pot_id': h.personal_a.id})
    inc = Income.query.one()
    assert inc.operation_id is None
    h.client_a.post(f'/incomes/{inc.id}/delete')
    assert Income.query.count() == 0


def test_pot_restored_when_leaving_common_category(db, household_ab):
    h = household_ab
    common_cat, perso_cat = _cat(db, h.common), _cat(db, h.personal_a, 'Perso')
    op = _op(db, h, h.personal_a, account=_account(db, h.personal_a))
    classify(op, h.user_a, 'category', common_cat)
    assert op.pot_id == h.common.id
    classify(op, h.user_a, 'off_budget')
    assert op.pot_id == h.personal_a.id
    assert spent_for_category(common_cat, 2026, 11) == 0.0
    classify(op, h.user_a, 'category', common_cat)
    classify(op, h.user_a, 'category', perso_cat)
    assert op.pot_id == h.personal_a.id
    assert spent_for_category(common_cat, 2026, 11) == 0.0
    assert spent_for_category(perso_cat, 2026, 11) == 40.0


def test_refund_on_common_category_then_income_goes_back_to_personal(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    op = _op(db, h, h.personal_a, direction='credit', amount=30.0,
             account=_account(db, h.personal_a))
    classify(op, h.user_a, 'refund', cat)
    assert op.pot_id == h.common.id
    assert spent_for_category(cat, 2026, 11) == -30.0
    classify(op, h.user_a, 'income')
    assert op.pot_id == h.personal_a.id
    assert Income.query.one().pot_id == h.personal_a.id
    assert spent_for_category(cat, 2026, 11) == 0.0
    assert _bm(h.personal_a.id, 2026, 11).total_income == 30.0


def test_recalculates_calendar_month_of_date_not_bank_date(db, household_ab):
    h = household_ab
    cat = _cat(db, h.personal_a)
    op = _op(db, h, h.personal_a, bank_date=date(2026, 12, 2))
    op.date = date(2026, 11, 30)
    db.session.commit()
    classify(op, h.user_a, 'category', cat)
    assert _bm(h.personal_a.id, 2026, 11) is not None


def test_classify_income_twice_is_idempotent(db, household_ab):
    h = household_ab
    op = _op(db, h, h.common, direction='credit', amount=500.0, account=_account(db, h.common))
    classify(op, h.user_a, 'income')
    classify(op, h.user_a, 'income')
    assert Income.query.count() == 1
    assert _bm(h.common.id, 2026, 11).total_income == 500.0


# --- J4: « Supplément remise à flot » ---

def _other_household_user(db):
    from app.models import User
    from app.services.household_service import create_household
    u = User(email='z@test.com', display_name='Z')
    u.set_password('x')
    create_household('Autre', u)
    return u


def test_classify_contribution_sets_contributor_no_income(db, household_ab):
    h = household_ab
    op = _op(db, h, h.common, direction='credit', amount=120.0, account=_account(db, h.common))
    incomes_before = Income.query.count()
    classify(op, h.user_a, 'contribution', contributor=h.user_b)
    assert (op.classification, op.contributor_id, op.category_id) == ('contribution', h.user_b.id, None)
    assert Income.query.count() == incomes_before
    bm = _bm(h.common.id, 2026, 11)
    assert bm is None or bm.total_income == 0.0


def test_contribution_debit_refused(db, household_ab):
    h = household_ab
    op = _op(db, h, h.common, direction='debit', account=_account(db, h.common))
    with pytest.raises(ClassificationError):
        classify(op, h.user_a, 'contribution', contributor=h.user_b)


def test_contribution_on_personal_account_refused(db, household_ab):
    h = household_ab
    op = _op(db, h, h.personal_a, direction='credit', account=_account(db, h.personal_a))
    with pytest.raises(ClassificationError):
        classify(op, h.user_a, 'contribution', contributor=h.user_a)


def test_contribution_contributor_required_and_same_household(db, household_ab):
    h = household_ab
    op = _op(db, h, h.common, direction='credit', account=_account(db, h.common))
    with pytest.raises(ClassificationError):
        classify(op, h.user_a, 'contribution')
    with pytest.raises(ClassificationError):
        classify(op, h.user_a, 'contribution', contributor=_other_household_user(db))


def test_income_to_contribution_removes_income(db, household_ab):
    h = household_ab
    op = _op(db, h, h.common, direction='credit', amount=200.0, account=_account(db, h.common))
    classify(op, h.user_a, 'income')
    assert Income.query.filter_by(operation_id=op.id).count() == 1
    classify(op, h.user_a, 'contribution', contributor=h.user_a)
    assert Income.query.filter_by(operation_id=op.id).count() == 0
    assert _bm(h.common.id, 2026, 11).total_income == 0.0


def test_reclassify_contribution_clears_contributor(db, household_ab):
    h = household_ab
    op = _op(db, h, h.common, direction='credit', account=_account(db, h.common))
    classify(op, h.user_a, 'contribution', contributor=h.user_b)
    classify(op, h.user_a, 'internal')
    assert op.contributor_id is None


def test_contribution_received_sums_month(db, household_ab):
    from app.services.pointage_service import contribution_received
    h = household_ab
    acc = _account(db, h.common)
    for amount, day, ref in ((50.0, date(2026, 11, 3), 'R1'), (30.0, date(2026, 11, 20), 'R2'),
                             (99.0, date(2026, 10, 28), 'R3')):
        op = _op(db, h, h.common, direction='credit', amount=amount, account=acc,
                 bank_date=day, bank_reference=ref)
        classify(op, h.user_a, 'contribution', contributor=h.user_b)
    assert contribution_received(h.common, h.user_b, 2026, 11) == pytest.approx(80.0)
    assert contribution_received(h.common, h.user_a, 2026, 11) == 0.0
