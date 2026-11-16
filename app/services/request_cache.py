"""Per-HTTP-request memo for budget figures recomputed many times by one page (dashboard, plan).

Values are floats keyed by a tuple, stored on `flask.g`. Any write through the session clears
the whole memo (flush, commit, rollback, bulk UPDATE/DELETE); unflushed changes bypass it, since
the SQL query it replaces would have autoflushed them first. No caching outside a request
(scheduler jobs, CLI)."""
from flask import g, has_request_context
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.extensions import db


def clear(*_args, **_kwargs):
    if has_request_context():
        g.pop('_req_cache', None)


def memo(key, compute):
    if not has_request_context():
        return compute()
    session = db.session
    if session.new or session.dirty or session.deleted:
        clear()
        return compute()
    cache = g.setdefault('_req_cache', {})
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def memo_object(key, model, compute):
    """Memo of a query returning one ORM object or None. The object itself is kept (a strong
    reference: the session's identity map only holds weak ones), safe since any write clears the
    memo. `model` documents the expected type."""
    return memo(key, compute)


def memo_objects(key, model, compute):
    """Same for a list of ORM objects; each call gets its own list (order kept)."""
    return list(memo(key, lambda: list(compute())))


def _on_execute(orm_execute_state):
    if not orm_execute_state.is_select:
        clear()


event.listen(Session, 'after_flush', clear)
event.listen(Session, 'after_commit', clear)
event.listen(Session, 'after_rollback', clear)
event.listen(Session, 'after_soft_rollback', clear)
event.listen(Session, 'do_orm_execute', _on_execute)
