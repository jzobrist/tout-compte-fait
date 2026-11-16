"""`Server-Timing` header (visible in the browser dev tools): SQL count/time and total server time
of each response, for logged-in users only. Durations and counts only, never data."""
import time

from flask import g, has_request_context
from sqlalchemy import event
from sqlalchemy.engine import Engine


def _before_cursor(conn, cursor, statement, parameters, context, executemany):
    if has_request_context() and '_st_start' in g:
        context._st_t0 = time.perf_counter()


def _after_cursor(conn, cursor, statement, parameters, context, executemany):
    t0 = getattr(context, '_st_t0', None)
    if t0 is not None and has_request_context() and '_st_start' in g:
        g._st_sql_n += 1
        g._st_sql_t += time.perf_counter() - t0


def install(app):
    if not event.contains(Engine, 'before_cursor_execute', _before_cursor):   # once per process,
        event.listen(Engine, 'before_cursor_execute', _before_cursor)        # not per create_app
        event.listen(Engine, 'after_cursor_execute', _after_cursor)

    @app.before_request
    def _start():
        g._st_start = time.perf_counter()
        g._st_sql_n = 0
        g._st_sql_t = 0.0

    @app.after_request
    def _header(response):
        from flask_login import current_user
        start = g.pop('_st_start', None)
        if start is None or not current_user.is_authenticated:
            return response
        total = (time.perf_counter() - start) * 1000
        response.headers['Server-Timing'] = (
            f'db;dur={g._st_sql_t * 1000:.1f};desc="SQL ({g._st_sql_n} req)", app;dur={total:.1f}')
        return response
