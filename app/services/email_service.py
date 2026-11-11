from flask_mail import Message
from app.extensions import mail

def send_alert_email(to_addresses, subject, body):
    """Send plain-text alert email to a list of addresses."""
    if not to_addresses:
        return
    msg = Message(subject=subject, recipients=to_addresses, body=body)
    mail.send(msg)
