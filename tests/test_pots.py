from app.models import User, Pot, Household
from app.seeds import COMMON_DEFAULT_CATEGORIES
from app.services.access_service import accessible_pots, can_access_pot


def _register(client, email='a@test.com', name='A', hh='Foyer'):
    return client.post('/register', data={
        'email': email, 'display_name': name,
        'password': 'Password1!', 'household_name': hh,
    })


def test_register_creates_common_and_personal_pots(client, db):
    _register(client)
    user = User.query.filter_by(email='a@test.com').one()
    pots = Pot.query.filter_by(household_id=user.household_id).all()
    assert len(pots) == 2
    common = next(p for p in pots if p.kind == 'common')
    personal = next(p for p in pots if p.kind == 'personal')
    assert common.name == 'Commun' and common.owner_id is None
    assert personal.owner_id == user.id and personal.name == 'Perso – A'
    assert {c.name for c in common.categories} == {r[0] for r in COMMON_DEFAULT_CATEGORIES}
    assert {c.name for c in personal.categories} == {
        'Virement vers le commun', 'Loisirs perso',
        'Abonnements perso', 'Épargne perso'}


def test_invite_join_creates_personal_pot_only(household_ab):
    h = household_ab
    pots = Pot.query.filter_by(household_id=h.user_a.household_id).all()
    assert len(pots) == 3
    assert sum(p.kind == 'common' for p in pots) == 1
    assert h.personal_b.owner_id == h.user_b.id
    assert h.personal_b.name == 'Perso – B'


def test_accessible_pots_and_can_access(household_ab, db):
    h = household_ab
    assert accessible_pots(h.user_a) == [h.common, h.personal_a]
    assert can_access_pot(h.user_a, h.personal_b) is False
    assert can_access_pot(h.user_b, h.personal_b) is True
    other = User(email='c@test.com', display_name='C', household_id=0)
    other.set_password('Password1!')
    from app.services.household_service import create_household
    create_household('Autre', other)
    assert can_access_pot(other, h.common) is False


def test_personal_defaults_values(household_ab):
    cats = {c.name: c for c in household_ab.personal_a.categories}
    c = cats['Loisirs perso']
    assert (c.type, c.percentage, c.carry_over) == ('dynamic', 0.15, True)
    c = cats['Épargne perso']
    assert (c.type, c.percentage, c.carry_over) == ('dynamic', 0.10, True)
    c = cats['Abonnements perso']
    assert (c.type, c.auto_day, c.carry_over) == ('fixed_auto', 5, False)
    c = cats['Virement vers le commun']
    assert (c.type, c.carry_over) == ('fixed_amount', False)
