from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_mail import Mail
from flask_bcrypt import Bcrypt
from flask_wtf.csrf import CSRFProtect
from apscheduler.schedulers.background import BackgroundScheduler

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
mail = Mail()
bcrypt = Bcrypt()
csrf = CSRFProtect()
scheduler = BackgroundScheduler()

login_manager.login_view = 'auth.login'
login_manager.login_message_category = 'info'
