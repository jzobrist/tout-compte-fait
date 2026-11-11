from app.extensions import db
from app.models import Household, Pot
from app.seeds import seed_categories
from app.services.category_service import attach_shared_families


def create_personal_pot(user, commit=True):
    """Create the user's personal pot (with default categories); commit unless told not to."""
    pot = Pot(
        household_id=user.household_id,
        kind='personal',
        owner_id=user.id,
        name=f'Perso – {user.display_name}',
    )
    db.session.add(pot)
    db.session.flush()
    attach_shared_families(pot)
    seed_categories(pot)
    if commit:
        db.session.commit()
    return pot


def create_household(name, user):
    """Create a household with its common pot and the user's personal pot.

    `user` must not be flushed yet; it is attached to the new household.
    """
    household = Household(name=name)
    db.session.add(household)
    db.session.flush()
    user.household_id = household.id
    db.session.add(user)
    common = Pot(household_id=household.id, kind='common', owner_id=None, name='Commun')
    db.session.add(common)
    db.session.flush()
    seed_categories(common)
    create_personal_pot(user)
    return household
