from app.models import Pot
import io
from datetime import date

import pytest

from app import create_app
from app.extensions import db as _db

CSV_HEADER = (
    'Date de comptabilisation;Libelle simplifie;Libelle operation;Reference;'
    'Informations complementaires;Type operation;Categorie;Sous categorie;'
    'Debit;Credit;Date operation;Date de valeur;Pointage operation\n'
)


def _row(ref, label, debit='', credit='', op_date='10/08/2026'):
    return f'{op_date};{label};{label};{ref};;Carte bancaire;;;{debit};{credit};{op_date};{op_date};0\n'


def _upload(client, content, encoding='utf-8', account_id=None):
    if account_id is None:
        account_id = _account().id
    return client.post(
        '/operations/import',
        data={'account_id': account_id,
              'csv_file': (io.BytesIO(content.encode(encoding)), 'releve.csv')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )


@pytest.fixture
def auth_client(client, db):
    client.post('/register', data={
        'email': 'imp@test.com', 'display_name': 'Imp',
        'password': 'Password1!', 'household_name': 'ImpFoyer',
    }, follow_redirects=True)
    return client


def _user():
    from app.models import User
    return User.query.filter_by(email='imp@test.com').first()


def _account():
    from app.models import BankAccount, Pot
    acc = BankAccount.query.first()
    if acc is None:
        pot = Pot.query.filter_by(household_id=_user().household_id, kind='common').one()
        acc = BankAccount(pot_id=pot.id, name='Courant', bank_name='BP')
        _db.session.add(acc)
        _db.session.commit()
    return acc


def test_import_creates_uncategorized_debits(auth_client, db):
    from app.models.operation import Operation
    content = CSV_HEADER + _row('REF1', 'FASTFOOD PARIS', debit='-40,30')
    resp = _upload(auth_client, content)
    assert resp.status_code == 200
    op = Operation.query.filter_by(bank_reference='REF1').one()
    assert op.category_id is None
    assert abs(op.amount - 40.30) < 0.001
    assert op.date == date(2026, 8, 10)
    assert op.label == 'FASTFOOD PARIS'


def test_import_sets_account_pot(auth_client, db):
    from app.models.operation import Operation
    _upload(auth_client, CSV_HEADER + _row('REFP', 'ACHAT', debit='-10,00'))
    op = Operation.query.filter_by(bank_reference='REFP').one()
    assert op.pot.kind == 'common' and op.bank_account_id == _account().id


def test_import_keeps_credits_and_skips_duplicates(auth_client, db):
    from app.models.operation import Operation
    content = (CSV_HEADER
               + _row('REF1', 'ACHAT', debit='-10,00')
               + _row('REF2', 'SALAIRE', credit='2000,00'))
    _upload(auth_client, content)
    resp = _upload(auth_client, content)  # second import: REF1 is a duplicate
    assert Operation.query.filter_by(bank_reference='REF1').count() == 1
    credit = Operation.query.filter_by(bank_reference='REF2').one()
    assert credit.direction == 'credit' and credit.amount == 2000.0
    assert 'doublon' in resp.get_data(as_text=True)


def test_import_accepts_latin1(auth_client, db):
    from app.models.operation import Operation
    content = CSV_HEADER + _row('REF9', 'CAFÉ DU COIN', debit='-3,50')
    _upload(auth_client, content, encoding='latin-1')
    assert Operation.query.filter_by(bank_reference='REF9').one().label == 'CAFÉ DU COIN'


def test_pointage_lists_only_own_uncategorized(auth_client, db):
    from app.models import Household, User
    from app.models.operation import Operation
    u = _user()
    from app.models import Pot
    from app.services.household_service import create_household
    other_user = User(email='o@test.com', display_name='O')
    other_user.set_password('Password1!')
    other = create_household('Autre', other_user)
    _db.session.commit()
    common = Pot.query.filter_by(household_id=u.household_id, kind='common').one()
    _db.session.add_all([
        Operation(pot_id=common.id, user_id=u.id, amount=5, label='A POINTER', bank_reference='ZZ1'),
        Operation(pot_id=other_user.household.pots[0].id, user_id=other_user.id, amount=7, label='PAS A MOI',
                  bank_reference='ZZ2'),
    ])
    _db.session.commit()
    html = auth_client.get('/operations/pointage').get_data(as_text=True)
    assert 'A POINTER' in html
    assert 'PAS A MOI' not in html


def test_classify_sets_category(auth_client, db):
    from app.models import Category
    from app.models.operation import Operation
    u = _user()
    cat = Category.query.join(Pot).filter(Pot.household_id == u.household_id).first()
    op = Operation(pot_id=cat.pot_id, user_id=u.id, amount=5, label='X', bank_reference='C1')
    _db.session.add(op)
    _db.session.commit()
    resp = auth_client.post(f'/operations/{op.id}/classify',
                            data={'classification': 'category', 'category_id': cat.id}, headers={'HX-Request': 'true'})
    assert resp.status_code == 200
    assert _db.session.get(Operation, op.id).category_id == cat.id


def test_classify_rejects_foreign_category(auth_client, db):
    from app.models import Household, Category
    from app.models.operation import Operation
    u = _user()
    from app.models import Pot, User
    from app.services.household_service import create_household
    other_user = User(email='o@test.com', display_name='O')
    other_user.set_password('Password1!')
    other = create_household('Autre', other_user)
    _db.session.commit()
    mine = Pot.query.filter_by(household_id=u.household_id, kind='common').one()
    foreign = Category(name='Etrangere', pot_id=other.pots[0].id,
                       type='fixed_amount', amount=10)
    op = Operation(pot_id=mine.id, user_id=u.id, amount=5, label='X', bank_reference='C2')
    _db.session.add_all([foreign, op])
    _db.session.commit()
    resp = auth_client.post(f'/operations/{op.id}/classify',
                            data={'classification': 'category', 'category_id': foreign.id})
    assert resp.status_code == 404
    assert _db.session.get(Operation, op.id).category_id is None


@pytest.fixture
def csrf_client():
    app = create_app('testing')
    app.config['WTF_CSRF_ENABLED'] = True
    with app.app_context():
        _db.create_all()
        yield app.test_client()
        _db.session.remove()
        _db.drop_all()


@pytest.mark.parametrize('path', ['/operations/import', '/operations/1/classify'])
def test_post_without_csrf_token_is_rejected(csrf_client, path):
    resp = csrf_client.post(path, data={})
    assert resp.status_code == 400


def test_import_page_renders_csrf_token(csrf_client):
    from app.models import Household, User
    from app.services.household_service import create_household
    u = User(email='c@test.com', display_name='C')
    u.set_password('Password1!')
    create_household('F', u)
    _db.session.commit()
    with csrf_client.session_transaction() as sess:
        sess['_user_id'] = str(u.id)
        sess['_fresh'] = True
    html = csrf_client.get('/operations/import').get_data(as_text=True)
    assert 'name="csrf_token" value="' in html
    assert 'value=""' not in html
