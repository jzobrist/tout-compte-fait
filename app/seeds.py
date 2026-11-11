from app.extensions import db
from app.models.category import Category

# (name, type, percentage, min_amount, max_amount, amount, carry_over, auto_day)
COMMON_DEFAULT_CATEGORIES = [
    # (name, type, percentage, min_amount, max_amount, amount, carry_over, auto_day)
    ('Loyer / Prêt immobilier', 'fixed_auto', None, None, None, None, False, 1),
    ('Énergie (élec, gaz)', 'fixed_auto', None, None, None, None, False, 5),
    ('Internet / Mobile', 'fixed_auto', None, None, None, None, False, 5),
    ('Assurances', 'fixed_auto', None, None, None, None, False, 5),
    ('Abonnements', 'fixed_auto', None, None, None, None, False, 5),
    ('Crédit conso / auto', 'fixed_auto', None, None, None, None, False, 5),
    ('Épargne automatique', 'fixed_auto', None, None, None, None, False, 1),
    ('Alimentation', 'fixed_amount', None, None, None, None, True, None),
    ('Transports', 'fixed_amount', None, None, None, None, True, None),
    ('Santé', 'fixed_amount', None, None, None, None, True, None),
    ('Entretien maison', 'fixed_amount', None, None, None, None, True, None),
    ('Vêtements', 'fixed_amount', None, None, None, None, True, None),
    ('Animaux', 'fixed_amount', None, None, None, None, True, None),
    ("Garde d'enfant (PAJEMPLOI)", 'fixed_amount', None, None, None, None, True, None),
    ('Loisirs (commun)', 'dynamic', 0.10, 30.0, None, None, True, None),
    ('Restaurants / Cafés', 'dynamic', 0.05, 20.0, None, None, True, None),
    ('Vacances / Voyages', 'dynamic', 0.08, 0.0, None, None, True, None),
    ('Cadeaux / Occasions', 'dynamic', 0.03, 0.0, None, None, True, None),
    ('Culture', 'dynamic', 0.03, 0.0, None, None, True, None),
    ('Épargne projet', 'dynamic', 0.05, 0.0, None, None, True, None),
]


COMMON_TRANSFER_NAME = 'Virement vers le commun'

PERSONAL_DEFAULT_CATEGORIES = [
    ('Virement vers le commun', 'fixed_amount', None, None, None, None, False, None),
    ('Loisirs perso', 'dynamic', 0.15, 0.0, None, None, True, None),
    ('Abonnements perso', 'fixed_auto', None, None, None, None, False, 5),
    ('Épargne perso', 'dynamic', 0.10, 0.0, None, None, True, None),
]


def seed_categories(pot):
    rows = COMMON_DEFAULT_CATEGORIES if pot.is_common else PERSONAL_DEFAULT_CATEGORIES
    present = {c.name.lower() for c in Category.query.filter_by(pot_id=pot.id)}
    for name, type_, pct, min_a, max_a, amount, carry, auto_day in rows:
        if name.lower() in present:
            continue
        db.session.add(Category(
            scope=None if pot.is_common else 'shared',
            role='common_transfer' if (not pot.is_common and name == COMMON_TRANSFER_NAME) else None,
            name=name,
            type=type_,
            percentage=pct,
            min_amount=min_a,
            max_amount=max_a,
            amount=amount,
            carry_over=carry,
            auto_day=auto_day,
            pot_id=pot.id,
        ))
