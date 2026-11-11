from urllib.parse import parse_qs, urlparse

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import BankAccountLink
from app.routes.forms import BankConnectForm, BankMapForm, BankPasteForm, BankSetupForm
from app.services import bank_sync_service
from app.services.access_service import accessible_pots, can_access_pot
from app.services.enablebanking_client import EnableBankingError
from app.services.secrets_service import SecretsError

bank_bp = Blueprint('bank', __name__, url_prefix='/bank')

UNKNOWN_STATE = 'Demande de connexion inconnue ou expirée.'
BANK_UNREACHABLE = 'La banque est injoignable ou a refusé la demande. Réessayez plus tard.'
MAX_PEM_BYTES = 16 * 1024


def _redirect_url():
    return current_app.config.get('BANK_REDIRECT_URL') or url_for('bank.callback', _external=True)


def _aspsp_names(conn):
    if conn is None:
        return []
    try:
        return sorted(a.get('name') for a in bank_sync_service.get_client(conn).aspsps() if a.get('name'))
    except Exception:  # never let the optional datalist break the page; nothing logged (secrets)
        return []


@bank_bp.route('', methods=['GET'])
@login_required
def index():
    conn = bank_sync_service.get_connection(current_user)
    links = []
    editing = conn is not None and (request.args.get('edit') == '1' or conn.status == 'setup')
    if conn is not None:
        # (link, account): the account is exposed only when the viewer can access its pot
        links = [(l, l.bank_account if can_access_pot(current_user, getattr(l.bank_account, 'pot', None)) else None)
                 for l in sorted(conn.links, key=lambda l: l.id)]
    return render_template(
        'bank/index.html', conn=conn, links=links,
        pots=accessible_pots(current_user),
        attachable=bank_sync_service.attachable_accounts(current_user) if conn is not None else [],
        editing=editing,
        aspsp_names=_aspsp_names(conn) if editing else [],
        setup_form=BankSetupForm(), connect_form=BankConnectForm(),
        paste_form=BankPasteForm(), map_form=BankMapForm(),
        redirect_url=_redirect_url(),
    )


@bank_bp.route('/setup', methods=['POST'])
@login_required
def setup():
    form = BankSetupForm()
    if not form.validate_on_submit():
        for errors in form.errors.values():
            for error in errors:
                flash(error, 'danger')
        return redirect(url_for('bank.index'))
    raw = form.pem.data.stream.read(MAX_PEM_BYTES + 1)  # in memory only, never written to disk
    try:
        if len(raw) > MAX_PEM_BYTES:
            raise ValueError('too large')
        bank_sync_service.save_setup(current_user, form.app_id.data, raw.decode('utf-8'),
                                     form.aspsp_name.data)
    except (ValueError, UnicodeDecodeError):
        flash('Ce fichier n\'est pas une clé privée RSA valide (non chiffrée, au format PEM).', 'danger')
    except SecretsError:
        flash('Le chiffrement des secrets n\'est pas configuré sur ce serveur.', 'danger')
    else:
        flash('Identifiants enregistrés. Vous pouvez connecter votre banque.', 'success')
    return redirect(url_for('bank.index'))


@bank_bp.route('/connect', methods=['POST'])
@login_required
def connect():
    if not BankConnectForm().validate_on_submit():
        return redirect(url_for('bank.index'))
    conn = bank_sync_service.get_connection(current_user)
    if conn is None:
        flash('Enregistrez d\'abord vos identifiants Enable Banking.', 'warning')
        return redirect(url_for('bank.index'))
    try:
        url = bank_sync_service.start_connection(conn, _redirect_url())
    except (EnableBankingError, SecretsError):
        flash(BANK_UNREACHABLE, 'danger')
        return redirect(url_for('bank.index'))
    return redirect(url)


def _finish(state, code):
    try:
        bank_sync_service.complete_connection(current_user, state, code)
    except PermissionError:
        flash(UNKNOWN_STATE, 'danger')
    except (EnableBankingError, SecretsError):
        flash(BANK_UNREACHABLE, 'danger')
    else:
        flash('Banque connectée. Rattachez vos comptes ci-dessous.', 'success')
    return redirect(url_for('bank.index'))


@bank_bp.route('/callback', methods=['GET'])
@login_required
def callback():
    return _finish(request.args.get('state', ''), request.args.get('code', ''))


@bank_bp.route('/callback-paste', methods=['POST'])
@login_required
def callback_paste():
    form = BankPasteForm()
    if not form.validate_on_submit():
        flash('Adresse de retour manquante.', 'danger')
        return redirect(url_for('bank.index'))
    params = parse_qs(urlparse(form.return_url.data.strip()).query)
    state = (params.get('state') or [''])[0]
    code = (params.get('code') or [''])[0]
    if not state or not code:
        flash('Adresse de retour invalide : « state » et « code » introuvables.', 'danger')
        return redirect(url_for('bank.index'))
    return _finish(state, code)


@bank_bp.route('/links/<int:link_id>/map', methods=['POST'])
@login_required
def map_link(link_id):
    link = db.session.get(BankAccountLink, link_id)
    conn = bank_sync_service.get_connection(current_user)
    if link is None or conn is None or link.connection_id != conn.id:
        abort(404)
    form = BankMapForm()
    if not form.validate_on_submit():
        flash('Formulaire invalide.', 'danger')
        return redirect(url_for('bank.index'))
    if not (form.ignore.data or form.pot_id.data or form.account_id.data):
        flash('Choisissez un pot ou un compte.', 'warning')
        return redirect(url_for('bank.index'))
    known = bank_sync_service.map_account(current_user, link, form.pot_id.data, form.name.data,
                                          bool(form.ignore.data), form.account_id.data or None)
    if known is not None:
        flash(f'Compte déjà connu : rattaché à {known.name}', 'info')
    else:
        flash('Compte ignoré.' if form.ignore.data else 'Compte rattaché.', 'success')
    return redirect(url_for('bank.index'))


@bank_bp.route('/delete', methods=['POST'])
@login_required
def delete():
    if BankConnectForm().validate_on_submit():
        bank_sync_service.delete_connection(current_user)
        flash('Connexion bancaire supprimée. Vos comptes et opérations sont conservés.', 'success')
    return redirect(url_for('bank.index'))


@bank_bp.route('/sync', methods=['POST'])
@login_required
def sync():
    if BankConnectForm().validate_on_submit():
        conn = bank_sync_service.get_connection(current_user)
        if conn is None or conn.status not in bank_sync_service.SYNCABLE:
            flash('Aucune connexion bancaire active.', 'warning')
        else:
            result = bank_sync_service.sync_connection(conn)  # always the viewer's own connection
            if result.errors:
                flash(result.errors[0], 'danger')
            else:
                flash(f'{result.imported} importée(s) — {result.auto_linked} rapprochée(s)'
                      f' — {result.to_classify} à classer', 'success')
    if request.form.get('next') == 'pointage':
        return redirect(url_for('operations.pointage'))
    return redirect(url_for('bank.index'))
