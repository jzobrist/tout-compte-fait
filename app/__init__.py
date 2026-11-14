import os
from flask import Flask
from .extensions import db, login_manager, migrate, mail, bcrypt, csrf, scheduler
from config import config

def create_app(config_name=None):
    if config_name is None:
        config_name = os.environ.get('APP_CONFIG', 'development')
    app = Flask(__name__)
    app.config.from_object(config[config_name])

    # Fail fast in production if required secrets are missing
    if config_name == 'production':
        if not app.config.get('SECRET_KEY') or app.config['SECRET_KEY'] == 'dev-secret-key-change-in-prod':
            raise RuntimeError("SECRET_KEY environment variable must be set in production")
        if not app.config.get('SQLALCHEMY_DATABASE_URI'):
            raise RuntimeError("DATABASE_URL environment variable must be set in production")
        if not app.config.get('BANK_SECRETS_KEY'):
            raise RuntimeError("BANK_SECRETS_KEY environment variable must be set in production")
        from cryptography.fernet import Fernet
        try:
            Fernet(app.config['BANK_SECRETS_KEY'])
        except (ValueError, TypeError):
            raise RuntimeError("BANK_SECRETS_KEY must be a valid Fernet key")

    if app.config.get('PROXY_FIX'):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    db.init_app(app)
    login_manager.init_app(app)

    # Import models so SQLAlchemy metadata is populated before migrate
    from . import models  # noqa: F401

    migrate.init_app(app, db)
    mail.init_app(app)
    bcrypt.init_app(app)
    csrf.init_app(app)

    from .routes.auth import auth_bp
    from .routes.dashboard import dashboard_bp
    from .routes.operations import operations_bp
    from .routes.incomes import incomes_bp
    from .routes.categories import categories_bp
    from .routes.adjustments import adjustments_bp
    from .routes.history import history_bp
    from .routes.settings import settings_bp
    from .routes.pots import pots_bp
    from .routes.accounts import accounts_bp
    from .routes.bank import bank_bp
    from .routes.plan import plan_bp
    from .routes.pwa import pwa_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(operations_bp)
    app.register_blueprint(incomes_bp)
    app.register_blueprint(categories_bp)
    app.register_blueprint(adjustments_bp)
    app.register_blueprint(history_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(pots_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(bank_bp)
    app.register_blueprint(plan_bp)
    app.register_blueprint(pwa_bp)

    @app.context_processor
    def inject_pots():
        from flask_login import current_user
        if not current_user.is_authenticated:
            return {}
        from .services.access_service import accessible_pots, get_current_pot
        return {'current_pot': get_current_pot(), 'accessible_pots': accessible_pots(current_user)}

    @app.context_processor
    def inject_bank_state():
        from flask_login import current_user
        if not current_user.is_authenticated:
            return {}
        from .services import bank_sync_service
        conn = bank_sync_service.get_connection(current_user)  # the viewer's own connection only
        return {'bank_banner': bank_sync_service.banner_state(current_user, conn=conn),
                'bank_sync_available': conn is not None and conn.status in bank_sync_service.SYNCABLE}

    @app.context_processor
    def inject_category_notices():
        from flask_login import current_user
        if not current_user.is_authenticated:
            return {}
        from .services import category_service

        def category_notices():
            """Messages of the viewer's unseen notices; calling it marks them seen (full pages only)."""
            notices = category_service.pending_notices(current_user)
            messages = [n.message for n in notices]
            if notices:
                category_service.mark_seen(notices)
            return messages
        return {'category_notices': category_notices}

    @app.template_global()
    def category_label(category):
        from .services.category_service import display_name
        return display_name(category)

    @app.template_global()
    def account_label(op):
        """'Account (Bank)' of an operation, or None when absent or in a pot the viewer cannot access."""
        from flask_login import current_user
        from .services.access_service import can_access_pot
        account = op.bank_account
        if account is None or not can_access_pot(current_user, account.pot):
            return None
        return f'{account.name} ({account.bank_name})'

    @app.template_filter('money')
    def money_filter(value):
        value = round(value, 2) + 0.0  # + 0.0 turns -0.0 into 0.0 (no « -0,00 »)
        return f"{value:,.2f}".replace(',', ' ').replace('.', ',')

    @app.template_filter('euros')
    def euros_filter(value):
        """Whole euros, no cents (ceilings and transfers): 1241.0 -> « 1 241 »."""
        return f"{int(round(value + 0.0)):,}".replace(',', ' ')

    if not app.testing:
        from .services.alert_service import run_daily_alerts
        scheduler.add_job(run_daily_alerts, 'cron', hour=8, args=[app])
        from .services.bank_sync_service import sync_all, check_expiries
        scheduler.add_job(sync_all, 'cron', hour=6, args=[app])
        scheduler.add_job(check_expiries, 'cron', hour=7, args=[app])
        if not scheduler.running:
            scheduler.start()

    return app
