"""Installable web app: manifest and a no-op service worker (no offline cache: banking data)."""
from flask import Blueprint, Response, jsonify, url_for

pwa_bp = Blueprint('pwa', __name__)

THEME_COLOR = '#4f46e5'

# Registered for installability only. It never caches anything and has no fetch handler,
# so every request goes to the network as if there were no service worker.
SERVICE_WORKER_JS = """self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
"""


@pwa_bp.route('/manifest.webmanifest')
def manifest():
    icon = lambda name, size, **extra: {'src': url_for('static', filename=f'icons/{name}'),  # noqa: E731
                                        'sizes': size, 'type': 'image/png', **extra}
    resp = jsonify({
        'name': 'Tout Compte Fait',
        'short_name': 'Tout Compte Fait',
        'description': 'Budget du foyer : pot commun, pots perso privés, synchro bancaire.',
        'lang': 'fr',
        'start_url': '/',
        'scope': '/',
        'display': 'standalone',
        'background_color': '#ffffff',
        'theme_color': THEME_COLOR,
        'icons': [
            icon('icon-192.png', '192x192'),
            icon('icon-512.png', '512x512'),
            icon('maskable-512.png', '512x512', purpose='maskable'),
        ],
    })
    resp.mimetype = 'application/manifest+json'
    return resp


@pwa_bp.route('/sw.js')
def service_worker():
    resp = Response(SERVICE_WORKER_JS, mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp
