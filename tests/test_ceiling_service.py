import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Category, EnvelopeCeiling, EnvelopePlan


def test_ceiling_rows_unique_and_deleted_with_category(db, household_clean):
    h = household_clean
    pot = h.personal_a
    cat = Category(name='Courses', type='dynamic', percentage=0.5, pot_id=pot.id, carry_over=False)
    db.session.add(cat)
    db.session.commit()
    plan = EnvelopePlan(pot_id=pot.id, year=2026, month=11)
    db.session.add(plan)
    db.session.commit()
    db.session.add(EnvelopeCeiling(plan_id=plan.id, category_id=cat.id, amount=120.0))
    db.session.commit()
    assert plan.validated_at is None
    assert [c.amount for c in plan.ceilings] == [120.0]

    db.session.add(EnvelopeCeiling(plan_id=plan.id, category_id=cat.id, amount=50.0))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()

    db.session.delete(cat)
    db.session.commit()
    assert EnvelopeCeiling.query.count() == 0
    assert EnvelopePlan.query.count() == 1
