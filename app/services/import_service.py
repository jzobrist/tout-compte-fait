import csv
import datetime
import io
from dataclasses import dataclass

from app.extensions import db
from app.models.operation import Operation
from app.services.reconcile_service import reconcile_movements


@dataclass
class MovementIn:
    reference: str
    booked_on: datetime.date
    amount: float
    direction: str
    label: str
    bank_label: str


@dataclass
class IngestResult:
    imported: int
    auto_linked: int
    to_classify: int
    skipped_dup: int


def ingest_movements(account, movements, user):
    """Create operations for new bank movements of `account`, then auto-reconcile the debits.

    Duplicates are detected against the references of that account (and within the batch).
    An empty reference is never deduplicated.
    """
    imported = skipped_dup = 0
    new_ops = []

    existing_refs = {
        ref for (ref,) in db.session.query(Operation.bank_reference)
        .filter(Operation.bank_account_id == account.id, Operation.bank_reference.isnot(None))
    }

    for mv in movements:
        if mv.reference and mv.reference in existing_refs:
            skipped_dup += 1
            continue
        op = Operation(
            pot_id=account.pot_id,
            bank_account_id=account.id,
            user_id=user.id,
            category_id=None,
            amount=mv.amount,
            date=mv.booked_on,
            label=mv.label[:200],
            bank_reference=mv.reference or None,
            direction=mv.direction,
            classification=None,
            bank_label=mv.bank_label[:200],
            bank_date=mv.booked_on,
        )
        db.session.add(op)
        if mv.direction == 'debit':
            new_ops.append(op)
        if mv.reference:
            existing_refs.add(mv.reference)
        imported += 1

    db.session.commit()
    result = reconcile_movements(new_ops)
    return IngestResult(imported, result.auto_linked, imported - result.auto_linked, skipped_dup)


def import_banque_populaire_csv(content, account, user):
    """Parse a Banque Populaire CSV into movements and ingest them into `account`."""
    reader = csv.DictReader(io.StringIO(content), delimiter=';')
    movements = []

    for row in reader:
        ref = (row.get('Reference') or '').strip()
        debit_raw = (row.get('Debit') or '').strip()
        credit_raw = (row.get('Credit') or '').strip()
        direction, amount_raw = ('debit', debit_raw) if debit_raw else ('credit', credit_raw)
        if not amount_raw:
            continue

        try:
            amount = abs(float(amount_raw.replace(',', '.')))
        except ValueError:
            continue

        date_str = (row.get('Date operation') or '').strip()
        try:
            op_date = datetime.datetime.strptime(date_str, '%d/%m/%Y').date()
        except ValueError:
            try:
                op_date = datetime.datetime.strptime(
                    (row.get('Date de comptabilisation') or '').strip(), '%d/%m/%Y'
                ).date()
            except ValueError:
                op_date = datetime.date.today()

        label = (row.get('Libelle operation') or row.get('Libelle simplifie') or '').strip()
        movements.append(MovementIn(ref, op_date, amount, direction, label, label))

    return ingest_movements(account, movements, user)
