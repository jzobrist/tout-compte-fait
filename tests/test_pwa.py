import json


def test_manifest_served_with_install_fields(client, db):
    resp = client.get('/manifest.webmanifest')
    assert resp.status_code == 200
    assert resp.mimetype == 'application/manifest+json'
    m = json.loads(resp.get_data(as_text=True))
    assert m['name'] == 'Tout Compte Fait'
    assert m['display'] == 'standalone'
    assert m['start_url'] == '/'
    sizes = {i['sizes'] for i in m['icons']}
    assert {'192x192', '512x512'} <= sizes
    assert any(i.get('purpose') == 'maskable' for i in m['icons'])
    for icon in m['icons']:
        assert client.get(icon['src']).status_code == 200


def test_pages_link_manifest_and_icons(client, db):
    html = client.get('/login').get_data(as_text=True)
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert 'rel="icon"' in html
    assert 'rel="apple-touch-icon"' in html
    assert 'name="theme-color"' in html


def test_service_worker_never_caches(client, db):
    resp = client.get('/sw.js')
    assert resp.status_code == 200
    assert resp.mimetype in ('application/javascript', 'text/javascript')
    assert resp.headers.get('Service-Worker-Allowed') is None or resp.headers['Service-Worker-Allowed'] == '/'
    body = resp.get_data(as_text=True)
    assert 'caches' not in body  # banking data: no offline cache
