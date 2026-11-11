"""Front-end libraries are served by the app itself: no third-party CDN on any page."""
import re


def test_pages_load_no_external_assets(client, db):
    html = client.get('/login').get_data(as_text=True)
    assert not re.search(r'(src|href)="https?://', html)
    for name in ('bootstrap.min.css', 'bootstrap.bundle.min.js', 'htmx.min.js', 'chart.umd.min.js'):
        assert f'/static/vendor/{name}' in html
        resp = client.get(f'/static/vendor/{name}')
        assert resp.status_code == 200 and len(resp.data) > 10_000
        resp.close()
