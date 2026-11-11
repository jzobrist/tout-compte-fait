import pytest

@pytest.fixture
def logged_in_client(client, db):
    client.post('/register', data={
        'email': 'dash@test.com',
        'display_name': 'Dash',
        'password': 'Password1!',
        'household_name': 'DashFoyer',
    }, follow_redirects=True)
    return client

def test_dashboard_requires_login(client, db):
    resp = client.get('/', follow_redirects=False)
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']

def test_dashboard_loads(logged_in_client, db):
    resp = logged_in_client.get('/')
    assert resp.status_code == 200
    assert 'Commun' in resp.get_data(as_text=True)

def test_dashboard_month_navigation(logged_in_client, db):
    resp = logged_in_client.get('/?year=2026&month=4')
    assert resp.status_code == 200
