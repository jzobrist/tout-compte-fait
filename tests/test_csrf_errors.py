"""Expired/missing CSRF token: no time limit, and htmx calls get a marked 400 the page can explain."""
from config import Config


def test_csrf_token_valid_for_whole_session():
    assert Config.WTF_CSRF_TIME_LIMIT is None


def _post_without_token(app, client, headers):
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        return client.post('/login', data={'email': 'x@example.com', 'password': 'x'}, headers=headers)
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_htmx_csrf_failure_is_marked(app, client):
    resp = _post_without_token(app, client, {'HX-Request': 'true'})
    assert resp.status_code == 400
    assert resp.headers.get('X-Error') == 'csrf'


def test_plain_csrf_failure_still_400(app, client):
    resp = _post_without_token(app, client, {})
    assert resp.status_code == 400
    assert resp.headers.get('X-Error') == 'csrf'
