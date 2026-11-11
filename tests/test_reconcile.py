from datetime import date

import pytest

from app.models import BankAccount, Category, Operation
from app.services.budget_service import spent_for_category
from app.services.import_service import import_banque_populaire_csv
from app.services.reconcile_service import (
    candidates_for, eligible_entries, link, reconcile_movements, unlink,
)

CSV_HEADER = (
    'Date de comptabilisation;Libelle simplifie;Libelle operation;Reference;'
    'Informations complementaires;Type operation;Categorie;Sous categorie;'
    'Debit;Credit;Date operation;Date de valeur;Pointage operation\n'
)


def _row(ref, label, debit='', credit='', op_date='08/11/2026'):
    return f'{op_date};{label};{label};{ref};;Carte bancaire;;;{debit};{credit};{op_date};{op_date};0\n'


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


def _entry(db, user, cat, amount=3.2, day=date(2026, 11, 8), **kw):
    op = Operation(pot_id=cat.pot_id, user_id=user.id, category_id=cat.id, amount=amount,
                   entered_amount=amount, date=day, label='saisie', direction='debit',
                   classification='category', **kw)
    db.session.add(op)
    db.session.commit()
    return op


def _movement(db, user, acc, ref='R1', amount=3.2, bank_date=date(2026, 11, 8)):
    op = Operation(pot_id=acc.pot_id, bank_account_id=acc.id, user_id=user.id, amount=amount,
                   date=bank_date, label='CB', direction='debit', classification=None,
                   bank_reference=ref, bank_label='CB', bank_date=bank_date)
    db.session.add(op)
    db.session.commit()
    return op


def test_entry_then_import_two_days_later_links(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.common)
    _entry(db, h.user_a, cat, amount=3.2, day=date(2026, 11, 6))
    res = import_banque_populaire_csv(CSV_HEADER + _row('R1', 'CB X', debit='-3,20'), acc, h.user_a)
    assert (res.imported, res.auto_linked, res.to_classify) == (1, 1, 0)
    ops = Operation.query.all()
    assert len(ops) == 1 and ops[0].bank_reference == 'R1' and ops[0].date == date(2026, 11, 6)
    assert spent_for_category(cat, 2026, 11) == pytest.approx(3.2)


def test_window_bounds(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.common)
    mv = _movement(db, h.user_a, acc, bank_date=date(2026, 11, 20))
    for d, expected in [(10, 1), (9, 0), (23, 1), (24, 0)]:
        Operation.query.filter(Operation.bank_reference.is_(None)).delete()
        db.session.commit()
        _entry(db, h.user_a, cat, day=date(2026, 11, d))
        mv.amount = 3.2
        assert len(candidates_for(mv)) == expected, d


def test_amount_compared_rounded(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.common)
    _entry(db, h.user_a, cat, amount=0.1 + 0.2)
    assert len(candidates_for(_movement(db, h.user_a, acc, amount=0.3))) == 1
    assert len(candidates_for(_movement(db, h.user_a, acc, ref='R2', amount=0.31))) == 0


def test_two_identical_entries_two_movements(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.common)
    e1 = _entry(db, h.user_a, cat)
    e2 = _entry(db, h.user_a, cat)
    m1 = _movement(db, h.user_a, acc, 'R1')
    res = reconcile_movements([m1])
    assert (res.auto_linked, res.conflicts) == (0, 1)
    assert m1.bank_reference == 'R1' and Operation.query.count() == 3
    link(e1, m1)
    m2 = _movement(db, h.user_a, acc, 'R2', bank_date=date(2026, 11, 9))
    res = reconcile_movements([m2])
    assert (res.auto_linked, res.conflicts) == (1, 0)
    assert e2.bank_reference == 'R2' and Operation.query.count() == 2


def test_linked_in_pass_no_longer_candidate(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.common)
    e = _entry(db, h.user_a, cat)
    m1 = _movement(db, h.user_a, acc, 'R1')
    m2 = _movement(db, h.user_a, acc, 'R2', bank_date=date(2026, 11, 9))
    res = reconcile_movements([m2, m1])
    assert (res.auto_linked, res.conflicts) == (1, 0)
    assert e.bank_reference == 'R1'
    assert Operation.query.filter_by(bank_reference='R2').one().is_to_classify


def test_common_entry_by_owner_links_personal_card_movement(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.personal_a)
    e = _entry(db, h.user_a, cat)
    mv = _movement(db, h.user_a, acc)
    assert candidates_for(mv) == [e]
    link(e, mv)
    assert e.pot_id == h.common.id and e.category_id == cat.id and e.bank_account_id == acc.id


def test_common_entry_by_other_member_not_candidate(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.personal_a)
    _entry(db, h.user_b, cat)
    mv = _movement(db, h.user_a, acc)
    assert candidates_for(mv) == []
    assert eligible_entries(mv, any_amount=True) == []


def test_eligible_entries_any_amount(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.common)
    e = _entry(db, h.user_a, cat, amount=50.0)
    mv = _movement(db, h.user_a, acc, amount=52.3)
    assert candidates_for(mv) == []
    assert eligible_entries(mv, any_amount=True) == [e]
    assert eligible_entries(mv, any_amount=False) == []


def _fixed_auto(db, pot, day=date(2026, 11, 5)):
    c = Category(name='Loyer', type='fixed_auto', amount=700.0, auto_day=5, pot_id=pot.id)
    db.session.add(c)
    db.session.commit()
    op = Operation(pot_id=pot.id, user_id=1, category_id=c.id, amount=700.0, entered_amount=700.0,
                   date=day, label='Loyer', auto_generated=True, direction='debit',
                   classification='category')
    return c, op


def test_fixed_auto_links_to_real_debit(db, household_ab):
    h = household_ab
    acc = _account(db, h.common)
    c, op = _fixed_auto(db, h.common)
    op.user_id = h.user_a.id
    db.session.add(op)
    db.session.commit()
    mv = _movement(db, h.user_a, acc, amount=700.0, bank_date=date(2026, 11, 6))
    res = reconcile_movements([mv])
    assert res.auto_linked == 1 and op.bank_reference == 'R1'


def test_fixed_auto_of_common_not_candidate_for_personal_account(db, household_ab):
    h = household_ab
    acc = _account(db, h.personal_a)
    c, op = _fixed_auto(db, h.common)
    op.user_id = h.user_a.id
    db.session.add(op)
    db.session.commit()
    mv = _movement(db, h.user_a, acc, amount=700.0, bank_date=date(2026, 11, 6))
    assert candidates_for(mv) == []


def test_credits_imported_not_reconciled(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.common)
    _entry(db, h.user_a, cat, amount=20.0)
    res = import_banque_populaire_csv(CSV_HEADER + _row('C1', 'VIREMENT', credit='20,00'), acc, h.user_a)
    assert (res.imported, res.auto_linked, res.to_classify) == (1, 0, 1)
    cr = Operation.query.filter_by(bank_reference='C1').one()
    assert cr.direction == 'credit' and cr.amount == 20.0 and cr.classification is None
    assert cr.date == date(2026, 11, 8) and cr.user_id == h.user_a.id


def test_reimport_after_link_is_duplicate(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.common)
    _entry(db, h.user_a, cat)
    content = CSV_HEADER + _row('R1', 'CB X', debit='-3,20')
    import_banque_populaire_csv(content, acc, h.user_a)
    res = import_banque_populaire_csv(content, acc, h.user_a)
    assert (res.imported, res.skipped_dup) == (0, 1)
    assert Operation.query.count() == 1


def test_unlink_restores_entered_amount(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.common)
    e = _entry(db, h.user_a, cat, amount=50.0)
    mv = _movement(db, h.user_a, acc, amount=52.3)
    link(e, mv)
    assert e.amount == 52.3 and e.entered_amount == 50.0
    assert spent_for_category(cat, 2026, 11) == pytest.approx(52.3)
    new = unlink(e)
    assert e.amount == 50.0 and e.is_pending and e.bank_account_id is None
    assert e.bank_label is None and e.bank_date is None
    assert new.bank_reference == 'R1' and new.amount == 52.3 and new.is_to_classify
    assert new.direction == 'debit' and new.pot_id == acc.pot_id and new.date == date(2026, 11, 8)
    assert spent_for_category(cat, 2026, 11) == pytest.approx(50.0)


def test_unlink_pure_movement_refused(db, household_ab):
    h = household_ab
    acc = _account(db, h.common)
    mv = _movement(db, h.user_a, acc)
    with pytest.raises(ValueError):
        unlink(mv)
    with pytest.raises(ValueError):
        unlink(_entry(db, h.user_a, _cat(db, h.common)))


def test_import_flash_counts(db, household_ab):
    h = household_ab
    cat = _cat(db, h.common)
    acc = _account(db, h.common)
    _entry(db, h.user_a, cat)
    import io
    content = (CSV_HEADER + _row('R1', 'CB X', debit='-3,20') + _row('R2', 'AUTRE', debit='-9,00')
               + _row('C1', 'VIR', credit='5,00'))
    h.client_a.post('/operations/import', data={
        'account_id': acc.id, 'csv_file': (io.BytesIO(content.encode()), 'r.csv')},
        content_type='multipart/form-data')
    resp = h.client_a.post('/operations/import', data={
        'account_id': acc.id, 'csv_file': (io.BytesIO(content.encode()), 'r.csv')},
        content_type='multipart/form-data', follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert '0 importée(s) — 0 rapprochée(s) automatiquement — 0 à classer — 3 doublon(s) ignoré(s)' in html
    Operation.query.filter(Operation.bank_reference.isnot(None)).delete()
    db.session.commit()
    _entry(db, h.user_a, cat)
    resp = h.client_a.post('/operations/import', data={
        'account_id': acc.id, 'csv_file': (io.BytesIO(content.encode()), 'r.csv')},
        content_type='multipart/form-data', follow_redirects=True)
    assert '3 importée(s) — 1 rapprochée(s) automatiquement — 2 à classer' in resp.get_data(as_text=True)
