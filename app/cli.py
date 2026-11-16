"""Flask CLI commands (`flask <command>`)."""
from datetime import date, datetime

import click


@click.command('reset-operations')
@click.option('--since', help='First day of the next sync (YYYY-MM-DD); default: 26th of last month.')
@click.option('--yes', is_flag=True, help='Do not ask for confirmation.')
@click.option('--no-sync', is_flag=True, help='Do not sync the bank connections afterwards.')
def reset_operations_command(since, yes, no_sync):
    """Delete every operation and derived data (configuration kept), then resync from SINCE."""
    from app.models import BankConnection
    from app.services import bank_sync_service, reset_service
    start = (datetime.strptime(since, '%Y-%m-%d').date() if since
             else reset_service.default_since(date.today()))
    click.echo(f'Première synchro à partir du {start.isoformat()}. Lignes supprimées :')
    for table, n in reset_service.counts().items():
        click.echo(f'  {table}: {n}')
    if not yes and not click.confirm('Supprimer définitivement ?', default=False):
        click.echo('Annulé.')
        return
    reset_service.reset_operations(start)
    click.echo('Supprimé.')
    if no_sync:
        return
    for conn in BankConnection.query.filter(BankConnection.status.in_(('active', 'error'))):
        result = bank_sync_service.sync_connection(conn)
        click.echo(f'Synchro connexion {conn.id} : {result.imported} importées, '
                   f'{result.to_classify} à classer, erreurs : {result.errors or "aucune"}')


@click.command('perf-profile')
@click.option('--email', required=True, help='User to render the page as.')
@click.option('--path', default='/plan', show_default=True, help='Page to render.')
@click.option('--top', default=25, show_default=True, help='Rows per table.')
def perf_profile_command(email, path, top):
    """Render PATH as EMAIL and print where its SQL queries come from (code locations only;
    every write is rolled back, mail suppressed)."""
    from flask import current_app
    from app.perf_profile import profile
    try:
        status, n, elapsed, sites, chains = profile(current_app._get_current_object(), email, path, top)
    except LookupError:
        raise click.ClickException('Utilisateur inconnu.')
    click.echo(f'{path} -> HTTP {status}, {n} requêtes SQL, {elapsed * 1000:.0f} ms')
    click.echo('\nPar emplacement :')
    for site, count in sites:
        click.echo(f'  {count:6d}  {site}')
    click.echo('\nPar chaîne d\'appel (emplacement < appelants) :')
    for chain, count in chains:
        click.echo(f'  {count:6d}  {chain}')


def register(app):
    app.cli.add_command(reset_operations_command)
    app.cli.add_command(perf_profile_command)
