import os


def _env_bool(name, default):
    return os.environ.get(name, default).lower() == 'true'

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-prod')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = True
    # Fernet key encrypting bank secrets; None in dev if unset (encrypt/decrypt then raise SecretsError)
    BANK_SECRETS_KEY = os.environ.get('BANK_SECRETS_KEY')
    # Bank sync callback URL; when unset, derived from the request (url_for bank.callback)
    BANK_REDIRECT_URL = os.environ.get('BANK_REDIRECT_URL')
    APP_BASE_URL = os.environ.get('APP_BASE_URL')  # used in e-mails sent from scheduler jobs
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'localhost')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 25))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'false').lower() == 'true'
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER', 'compta@localhost')
    # Public « Créer un foyer » sign-up; when closed, only invite links work (the very first account is always allowed)
    ALLOW_REGISTRATION = _env_bool('ALLOW_REGISTRATION', 'true')
    # Trust X-Forwarded-For/-Proto from exactly one reverse proxy (Nginx) so external URLs use https
    PROXY_FIX = False

class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///tcf_dev.db'

class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    BANK_SECRETS_KEY = '72bAoVYA7-JvULR4t9sgvLNNUQjSP_yTdCQ9HtHnnD8='  # fixed test-only Fernet key

class ProductionConfig(Config):
    SECRET_KEY = os.environ.get('SECRET_KEY')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL')
    ALLOW_REGISTRATION = _env_bool('ALLOW_REGISTRATION', 'false')
    PROXY_FIX = True
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True

config = {
    'development': DevelopmentConfig,
    'testing': TestingConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig,
}
