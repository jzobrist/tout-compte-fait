import hashlib
import re

_CARD_LINE = re.compile(r'^\d{6} CB\*{4}\d{4}$')
_MAX = 200
_MAX_REF = 50


def reconstruct(lines):
    """Readable label from bank remittance lines (card date/number and FX lines dropped)."""
    kept = [l for l in lines if not _CARD_LINE.match(l.strip()) and 'EUR 1 EURO =' not in l]
    return re.sub(r'\s+', ' ', ' '.join(kept)).strip()[:_MAX]


def raw(lines):
    return ' | '.join(lines)[:_MAX]


def reference_for(tx):
    """Stable movement reference: entry_reference, then transaction_id, then a content hash."""
    ref = tx.get('entry_reference') or tx.get('transaction_id')
    if ref:
        ref = str(ref)
        if len(ref) > _MAX_REF:  # Operation.bank_reference is String(50)
            return 'h:' + hashlib.sha256(ref.encode()).hexdigest()[:40]
        return ref
    amount = (tx.get('transaction_amount') or {}).get('amount')
    direction = tx.get('credit_debit_indicator')
    raw_label = raw(tx.get('remittance_information') or [])
    digest = hashlib.sha256(
        f"{tx.get('booking_date')}|{amount}|{direction}|{raw_label}".encode()
    ).hexdigest()
    return 'h:' + digest[:40]
