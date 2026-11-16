import re


def _parse(header):
    """{'db': (dur, desc), 'app': (dur, desc)} from a Server-Timing header."""
    out = {}
    for part in header.split(','):
        fields = [f.strip() for f in part.split(';')]
        dur = next((float(f[4:]) for f in fields if f.startswith('dur=')), None)
        desc = next((f[5:].strip('"') for f in fields if f.startswith('desc=')), '')
        out[fields[0]] = (dur, desc)
    return out


def test_header_on_authenticated_page(db, household_clean):
    r = household_clean.client_a.get('/')
    timing = _parse(r.headers['Server-Timing'])
    assert set(timing) == {'db', 'app'}
    db_dur, db_desc = timing['db']
    app_dur, _ = timing['app']
    assert 0 <= db_dur <= app_dur
    assert re.fullmatch(r'SQL \(\d+ req\)', db_desc)
    assert int(re.search(r'\d+', db_desc).group()) > 0


def test_count_is_per_request(db, household_clean):
    c = household_clean.client_a
    def count():
        return int(re.search(r'\((\d+) req', c.get('/operations/pointage').headers['Server-Timing']).group(1))
    count()                            # first load may create the month rows
    n2, n3 = count(), count()
    assert n2 == n3 > 0                # not cumulative across requests


def test_no_header_when_anonymous(client, db):
    r = client.get('/login')
    assert 'Server-Timing' not in r.headers


def test_count_not_doubled_by_a_second_app(db, household_clean):
    import re
    from sqlalchemy import event
    from app import create_app
    create_app('testing')                     # a second app in the same process (some tests do this)
    real = {'n': 0}

    def count(*_):
        real['n'] += 1
    event.listen(db.engine, 'before_cursor_execute', count)
    try:
        header = household_clean.client_a.get('/operations/pointage').headers['Server-Timing']
    finally:
        event.remove(db.engine, 'before_cursor_execute', count)
    assert int(re.search(r'\((\d+) req', header).group(1)) == real['n']
