from flask import abort

from app.models import BankAccountLink, Operation
from app.services.access_service import can_access_pot


class ForeignLinkError(Exception):
    """Another member's bank link feeds this account: it cannot go to a personal pot."""


def move_account(user, account, target_pot):
    """Move a bank account to another pot the user can access.

    Operations still to classify follow the account; classified or reconciled ones stay in the
    pot where they were classified (they carry budget data). Pending movements are linked by
    account, so they follow implicitly. To-classify operations have no category, so no budget
    month needs recalculating.
    """
    if not (can_access_pot(user, account.pot) and can_access_pot(user, target_pot)):
        abort(404)
    if account.pot_id == target_pot.id:
        return
    if target_pot.kind == 'personal':
        foreign = [l for l in BankAccountLink.query.filter_by(bank_account_id=account.id, orphaned=False)
                   if l.connection.user_id != user.id]
        if foreign:
            raise ForeignLinkError()
    for op in Operation.query.filter_by(bank_account_id=account.id, pot_id=account.pot_id).all():
        if op.is_to_classify:
            op.pot_id = target_pot.id
    account.pot_id = target_pot.id
