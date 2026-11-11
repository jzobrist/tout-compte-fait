from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired
from wtforms import HiddenField, StringField, PasswordField, FloatField, SelectField, BooleanField, IntegerField, DateField
from wtforms.validators import InputRequired, DataRequired, Email, Length, Optional, NumberRange
import datetime

class RegisterForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    display_name = StringField('Nom affiché', validators=[DataRequired(), Length(max=80)])
    password = PasswordField('Mot de passe', validators=[DataRequired(), Length(min=8)])
    household_name = StringField('Nom du foyer', validators=[DataRequired()])

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Mot de passe', validators=[DataRequired()])

class JoinHouseholdForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    display_name = StringField('Nom affiché', validators=[DataRequired(), Length(max=80)])
    password = PasswordField('Mot de passe', validators=[DataRequired(), Length(min=8)])

class OperationForm(FlaskForm):
    amount = FloatField('Montant (€)', validators=[DataRequired(), NumberRange(min=0.01)])
    category_id = SelectField('Catégorie', coerce=int, validators=[DataRequired()])
    label = StringField('Description', validators=[Optional(), Length(max=200)])
    date = DateField('Date', validators=[DataRequired()], default=datetime.date.today)

class IncomeForm(FlaskForm):
    pot_id = HiddenField()
    amount = FloatField('Montant (€)', validators=[DataRequired(), NumberRange(min=0.01)])
    label = StringField('Description', validators=[DataRequired(), Length(max=200)])

class CategoryForm(FlaskForm):
    name = StringField('Nom', validators=[DataRequired(), Length(max=120)])
    type = SelectField('Type', choices=[
        ('fixed_auto', 'Charge fixe (prélèvement)'),
        ('fixed_amount', 'Enveloppe fixe'),
        ('dynamic', 'Dynamique (%)')
    ], validators=[DataRequired()])
    common = BooleanField('Commun')
    personal = SelectField('Perso', choices=[
        ('', 'Non'), ('shared', 'Pour nous deux'), ('own', 'Juste pour moi')], default='')


class CategorySettingsForm(FlaskForm):
    """Settings of one exemplar; instantiated with prefix 'common' or 'personal' (fields common-amount…)."""
    amount = FloatField('Montant mensuel (€)', validators=[Optional()])
    percentage = FloatField('Pourcentage du disponible (ex: 0.15)', validators=[Optional()])
    min_amount = FloatField('Plancher min (€)', validators=[Optional()])
    max_amount = FloatField('Plafond max (€)', validators=[Optional()])
    carry_over = BooleanField('Reporter le solde au mois suivant', default=True)
    constrained = BooleanField('Charge contrainte (compte dans le reste à vivre)', default=False)
    provision = BooleanField('Provision (peut recevoir le surplus du commun)', default=False)

    def values(self):
        return {k: getattr(self, k).data for k in
                ('amount', 'percentage', 'min_amount', 'max_amount', 'carry_over', 'constrained', 'provision')}

class AdjustmentForm(FlaskForm):
    from_category_id = SelectField('Depuis', coerce=int, validators=[DataRequired()])
    to_category_id = SelectField('Vers', coerce=int, validators=[DataRequired()])
    amount = FloatField('Montant (€)', validators=[DataRequired(), NumberRange(min=0.01)])


class BankAccountForm(FlaskForm):
    pot_id = HiddenField()
    name = StringField('Nom', validators=[DataRequired(), Length(max=120)])
    bank_name = StringField('Banque', validators=[DataRequired(), Length(max=120)])
    account_number_hint = StringField('Repère du numéro (IBAN, fin de numéro…)', validators=[Optional(), Length(max=34)])
    overdraft_limit = FloatField('Découvert autorisé (€)', default=0, validators=[Optional(), NumberRange(min=0)])


class BankSetupForm(FlaskForm):
    app_id = StringField('Identifiant d\'application (app_id)', validators=[DataRequired(), Length(max=64)])
    pem = FileField('Clé privée (.pem)', validators=[FileRequired('Fichier de clé privée requis.')])
    aspsp_name = StringField('Banque', validators=[DataRequired(), Length(max=120)])


class BankConnectForm(FlaskForm):
    """CSRF-only form for connect / delete."""


class BankPasteForm(FlaskForm):
    return_url = StringField('Adresse de retour', validators=[DataRequired(), Length(max=2000)])


class BankMapForm(FlaskForm):
    pot_id = HiddenField()
    account_id = HiddenField()
    name = StringField('Nom', validators=[Optional(), Length(max=120)])
    ignore = BooleanField('Ignorer ce compte')


class HouseholdSettingsForm(FlaskForm):
    solidarity_margin = FloatField(
        'Marge de sécurité découvert (€)',
        validators=[InputRequired(), NumberRange(min=0, message='La marge doit être positive ou nulle.')])
