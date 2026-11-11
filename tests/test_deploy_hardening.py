import pytest

from app import create_app
from app.models import User

REG = {'email': 'x@test.com', 'display_name': 'X', 'password': 'Password1!', 'household_name': 'F'}


def _register(client, email):
    return client.post('/register', data={**REG, 'email': email})


def test_register_closed_returns_404_once_a_user_exists(client, db, app, monkeypatch):
    _register(client, 'first@test.com')
    client.get('/logout')
    monkeypatch.setitem(app.config, 'ALLOW_REGISTRATION', False)
    assert client.get('/register').status_code == 404
    assert _register(client, 'second@test.com').status_code == 404
    assert User.query.filter_by(email='second@test.com').first() is None


def test_register_closed_still_allows_first_user(client, db, app, monkeypatch):
    monkeypatch.setitem(app.config, 'ALLOW_REGISTRATION', False)
    assert client.get('/register').status_code == 200
    _register(client, 'boot@test.com')
    assert User.query.filter_by(email='boot@test.com').first() is not None


def test_login_hides_register_link_when_closed(client, db, app, monkeypatch):
    _register(client, 'first@test.com')
    client.get('/logout')
    monkeypatch.setitem(app.config, 'ALLOW_REGISTRATION', False)
    assert 'Créer un foyer' not in client.get('/login').get_data(as_text=True)
    monkeypatch.setitem(app.config, 'ALLOW_REGISTRATION', True)
    assert 'Créer un foyer' in client.get('/login').get_data(as_text=True)


def test_invite_join_works_when_registration_closed(client, db, app, monkeypatch):
    _register(client, 'owner@test.com')
    owner = User.query.filter_by(email='owner@test.com').first()
    owner.household.generate_invite()
    db.session.commit()
    token = owner.household.invite_code
    client.get('/logout')
    monkeypatch.setitem(app.config, 'ALLOW_REGISTRATION', False)
    resp = client.post(f'/invite/{token}', data={'email': 'partner@test.com', 'display_name': 'P',
                                                 'password': 'Password1!'})
    assert resp.status_code == 302
    assert User.query.filter_by(email='partner@test.com').first() is not None


@pytest.fixture
def prod_env(monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'prod-test')
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///:memory:')
    monkeypatch.setenv('BANK_SECRETS_KEY', '72bAoVYA7-JvULR4t9sgvLNNUQjSP_yTdCQ9HtHnnD8=')
    monkeypatch.delenv('ALLOW_REGISTRATION', raising=False)
    import importlib
    import config
    importlib.reload(config)
    yield config
    importlib.reload(config)


def test_production_defaults(prod_env):
    cfg = prod_env.ProductionConfig
    assert cfg.SESSION_COOKIE_SECURE is True
    assert cfg.REMEMBER_COOKIE_SECURE is True
    assert cfg.ALLOW_REGISTRATION is False


def test_production_enables_proxy_fix(prod_env):
    assert prod_env.ProductionConfig.PROXY_FIX is True


def test_proxy_fix_trusts_one_proxy(monkeypatch):
    import config as config_mod

    class ProxiedTesting(config_mod.TestingConfig):
        PROXY_FIX = True

    import app as app_pkg  # create_app reads the dict it imported (config may have been reloaded)
    monkeypatch.setitem(app_pkg.config, 'proxied-testing', ProxiedTesting)
    proxied = create_app('proxied-testing')
    environ = {}

    def capture(env, start_response):
        environ.update(env)
        start_response('200 OK', [])
        return [b'']

    proxied.wsgi_app.app = capture
    proxied.wsgi_app({'REQUEST_METHOD': 'GET', 'PATH_INFO': '/', 'SERVER_NAME': 'budget.example.com',
                      'SERVER_PORT': '80', 'wsgi.url_scheme': 'http', 'HTTP_HOST': 'budget.example.com',
                      'HTTP_X_FORWARDED_PROTO': 'https', 'HTTP_X_FORWARDED_FOR': '1.2.3.4',
                      'REMOTE_ADDR': '127.0.0.1'}, lambda *a: None)
    assert environ['wsgi.url_scheme'] == 'https'
    assert environ['REMOTE_ADDR'] == '1.2.3.4'


def test_no_proxy_fix_outside_production(app):
    from werkzeug.middleware.proxy_fix import ProxyFix
    assert not isinstance(app.wsgi_app, ProxyFix)
