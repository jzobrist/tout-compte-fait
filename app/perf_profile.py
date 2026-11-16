"""`flask perf-profile`: render one page as a user and print where its SQL queries come from.

Prints code locations and counts only (never data). Everything the page writes is rolled back:
the session runs inside an outer transaction, its commits become savepoints. Mail is suppressed."""
import collections
import time
import traceback

from sqlalchemy import event
from sqlalchemy.orm import scoped_session, sessionmaker

_SKIP = ('request_cache.py', 'server_timing.py', 'perf_profile.py')


def _app_frames():
    frames = []
    for fr in traceback.extract_stack():
        if '/app/' not in fr.filename or fr.filename.endswith(_SKIP) or fr.name.startswith('<'):
            continue
        frames.append(f"{fr.filename.split('/app/', 1)[1]}:{fr.lineno} {fr.name}")
    return frames


def profile(app, email, path, top=25):
    """Return (status, n_queries, elapsed_s, by_site, by_chain); nothing is persisted."""
    from app.extensions import db
    from app.models import User

    user = User.query.filter_by(email=email).first()
    if user is None:
        raise LookupError(email)
    user_id = user.id
    db.session.remove()

    connection = db.engine.connect()
    sqlite_conn, old_isolation = None, None
    if connection.dialect.name == 'sqlite':
        # pysqlite issues no BEGIN of its own, so savepoints/rollback would not nest (documented
        # SQLAlchemy recipe): drive the transaction ourselves on this connection
        sqlite_conn = connection.connection.driver_connection
        old_isolation = sqlite_conn.isolation_level
        sqlite_conn.isolation_level = None
        event.listen(connection, 'begin', lambda conn: conn.exec_driver_sql('BEGIN'))
    outer = connection.begin()
    old_session = db.session
    # plain session bound to the outer transaction (Flask-SQLAlchemy's own Session.get_bind would
    # pick the engine and escape it); its commits become savepoints
    db.session = scoped_session(sessionmaker(bind=connection, join_transaction_mode='create_savepoint',
                                             query_cls=db.Query))
    mail_state = app.extensions.get('mail')
    old_suppress = getattr(mail_state, 'suppress', None)
    if mail_state is not None:
        mail_state.suppress = True

    by_site, by_chain, n = collections.Counter(), collections.Counter(), [0]

    escaped = [0]

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        if conn is not connection:
            escaped[0] += 1            # a query outside the rolled-back transaction: refuse to go on
            return
        n[0] += 1
        frames = _app_frames()
        if frames:
            by_site[frames[-1]] += 1
            by_chain[' < '.join(reversed(frames[-4:]))] += 1

    event.listen(db.engine, 'before_cursor_execute', on_execute)
    try:
        client = app.test_client()
        with client.session_transaction() as s:
            s['_user_id'] = str(user_id)
            s['_fresh'] = True
        client.get(path)                       # warm-up (first load of the month)
        n[0] = 0
        by_site.clear()
        by_chain.clear()
        t0 = time.perf_counter()
        response = client.get(path)
        elapsed = time.perf_counter() - t0
        if escaped[0]:
            raise RuntimeError(f'{escaped[0]} requêtes hors transaction')
    finally:
        event.remove(db.engine, 'before_cursor_execute', on_execute)
        db.session.remove()
        db.session = old_session
        outer.rollback()
        if sqlite_conn is not None:
            sqlite_conn.isolation_level = old_isolation
        connection.close()
        if mail_state is not None:
            mail_state.suppress = old_suppress
    return response.status_code, n[0], elapsed, by_site.most_common(top), by_chain.most_common(top)
