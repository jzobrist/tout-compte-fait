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


def register(app):
    app.cli.add_command(reset_operations_command)
