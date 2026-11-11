from datetime import date

from app.models import BankAccount, Category, Operation
from app.services.bank_labels import raw, reconstruct, reference_for
from app.services.import_service import IngestResult, MovementIn, ingest_movements


def test_reconstruct_card():
    assert reconstruct(["091126 CB****1234", "SUPERMARCHE 0001      FR 750001PARIS",
                        "15,17EUR 1 EURO = 1,000000"]) == "SUPERMARCHE 0001 FR 750001PARIS"


def test_reconstruct_sepa():
    assert reconstruct(["PRLV SEPA MUTUELLE", "ECH/101126 ID EMETTEUR/FR12ZZZ"]) == \
        "PRLV SEPA MUTUELLE ECH/101126 ID EMETTEUR/FR12ZZZ"


def test_reconstruct_transfer():
    assert reconstruct(["VIR M JEAN DUPONT"]) == "VIR M JEAN DUPONT"


def test_reconstruct_max_200():
    assert len(reconstruct(["A" * 150, "B" * 150])) == 200


def test_raw_joins_and_truncates():
    assert raw(["A", "B"]) == "A | B"
    assert len(raw(["x" * 300])) == 200


def test_reference_fallbacks():
    assert reference_for({"entry_reference": "E1", "transaction_id": "T1"}) == "E1"
    assert reference_for({"entry_reference": None, "transaction_id": "T1"}) == "T1"
    tx = {"booking_date": "2026-11-09", "transaction_amount": {"amount": "12.50"},
          "credit_debit_indicator": "DBIT", "remittance_information": ["SUPERMARCHE", "X"]}
    ref = reference_for(tx)
    assert ref.startswith("h:") and len(ref) == 42
    assert reference_for(dict(tx)) == ref
    other = dict(tx, booking_date="2026-11-10")
    assert reference_for(other) != ref


def _setup(db, household_ab):
    h = household_ab
    acc = BankAccount(pot_id=h.common.id, name='Compte', bank_name='Banque')
    db.session.add(acc)
    cat = Category(name='Courses', type='fixed_amount', amount=100.0, pot_id=h.common.id)
    db.session.add(cat)
    db.session.commit()
    entry = Operation(pot_id=cat.pot_id, user_id=h.user_a.id, category_id=cat.id, amount=12.5,
                      entered_amount=12.5, date=date(2026, 11, 9), label='saisie',
                      direction='debit', classification='category')
    db.session.add(entry)
    db.session.commit()
    return acc, entry


def test_ingest_dedups_and_reconciles(db, household_ab):
    acc, entry = _setup(db, household_ab)
    mv = MovementIn('R1', date(2026, 11, 9), 12.5, 'debit', 'SUPERMARCHE', 'SUPERMARCHE 0001')
    res = ingest_movements(acc, [mv], household_ab.user_a)
    assert isinstance(res, IngestResult)
    assert (res.imported, res.auto_linked, res.to_classify, res.skipped_dup) == (1, 1, 0, 0)
    op = Operation.query.filter_by(bank_reference='R1').one()
    assert op.bank_label == 'SUPERMARCHE 0001' and op.bank_date == date(2026, 11, 9)
    assert op.pot_id == acc.pot_id
    res2 = ingest_movements(acc, [mv], household_ab.user_a)
    assert (res2.imported, res2.skipped_dup) == (0, 1)


def test_ingest_dedups_within_batch(db, household_ab):
    acc, _ = _setup(db, household_ab)
    mv = MovementIn('R9', date(2026, 11, 1), 99.0, 'credit', 'VIR', 'VIR')
    res = ingest_movements(acc, [mv, mv], household_ab.user_a)
    assert (res.imported, res.skipped_dup) == (1, 1)
    assert Operation.query.filter_by(bank_reference='R9').count() == 1


def test_csv_import_goes_through_ingest(db, household_ab):
    from app.services.import_service import import_banque_populaire_csv
    acc, _ = _setup(db, household_ab)
    csv = ('Date de comptabilisation;Libelle simplifie;Libelle operation;Reference;x;y;z;w;'
           'Debit;Credit;Date operation\n'
           '09/11/2026;L;SUPERMARCHE;C1;;;;;-12,50;;09/11/2026\n')
    res = import_banque_populaire_csv(csv, acc, household_ab.user_a)
    assert isinstance(res, IngestResult) and res.imported == 1 and res.auto_linked == 1
