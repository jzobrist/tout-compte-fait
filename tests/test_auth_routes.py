from app.models import Pot
def test_register_creates_user_and_household(client, db):
    resp = client.post('/register', data={
        'email': 'new@test.com',
        'display_name': 'New User',
        'password': 'Password1!',
        'household_name': 'Mon Foyer',
    }, follow_redirects=True)
    assert resp.status_code == 200
    from app.models import User, Household
    u = User.query.filter_by(email='new@test.com').first()
    assert u is not None
    assert u.household.name == 'Mon Foyer'

def test_register_seeds_default_categories(client, db):
    from app.models import User, Category
    client.post('/register', data={
        'email': 'seed@test.com',
        'display_name': 'Seed',
        'password': 'Password1!',
        'household_name': 'SeedFoyer',
    }, follow_redirects=True)
    u = User.query.filter_by(email='seed@test.com').first()
    cats = Category.query.join(Pot).filter(Pot.household_id == u.household_id).all()
    assert len(cats) == 24  # 20 common + 4 personal default categories

def test_login_logout(client, db):
    client.post('/register', data={
        'email': 'login@test.com', 'display_name': 'Login',
        'password': 'Password1!', 'household_name': 'Foyer',
    })
    resp = client.post('/login', data={
        'email': 'login@test.com', 'password': 'Password1!'
    }, follow_redirects=True)
    assert resp.status_code == 200
    resp = client.get('/logout', follow_redirects=True)
    assert resp.status_code == 200

def test_invalid_login(client, db):
    resp = client.post('/login', data={
        'email': 'nobody@test.com', 'password': 'wrong'
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'invalide' in resp.data.lower() or b'invalid' in resp.data.lower()


def test_join_is_atomic_when_personal_pot_creation_fails(app, client, db, monkeypatch):
    from app.models import Household, User
    import app.routes.auth as auth_mod
    client.post('/register', data={
        'email': 'a@test.com', 'display_name': 'A',
        'password': 'Password1!', 'household_name': 'Foyer'})
    client.get('/logout')
    household = Household.query.first()
    household.generate_invite()
    db.session.commit()
    token = household.invite_code

    def boom(user, commit=True):
        raise RuntimeError('boom')
    monkeypatch.setattr(auth_mod, 'create_personal_pot', boom)
    joiner = app.test_client()
    try:
        joiner.post(f'/invite/{token}', data={
            'email': 'b@test.com', 'display_name': 'B', 'password': 'Password1!'})
    except RuntimeError:
        pass
    db.session.rollback()
    assert User.query.filter_by(email='b@test.com').first() is None
    assert db.session.get(Household, household.id).is_invite_valid()
