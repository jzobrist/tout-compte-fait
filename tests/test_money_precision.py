"""Money columns must be double precision on MariaDB, and gauges compare to the cent."""
from types import SimpleNamespace

import pytest
from flask import render_template
from sqlalchemy.dialects import mysql

from app.extensions import db as _db


def test_no_single_precision_float_column_on_mysql(app):
    dialect = mysql.dialect()
    singles = [f'{t.name}.{c.name}' for t in _db.metadata.sorted_tables for c in t.columns
               if c.type.compile(dialect=dialect) == 'FLOAT']
    assert singles == []


def _card(app, spent, effective):
    item = SimpleNamespace(
        category=SimpleNamespace(id=1, name='Internet / Mobile', type='fixed_auto'),
        metrics={'spent': spent, 'effective': effective, 'remaining_daily': 0.0,
                 'balance': effective - spent})
    with app.test_request_context('/'):
        return render_template('dashboard/_category_card.html', item=item, year=2026, month=11)


@pytest.mark.parametrize('spent', [37.970001220703125, 37.96999931335449])
def test_gauge_exactly_spent_is_warn_not_danger(app, spent):
    html = _card(app, spent, 37.97)
    assert 'cat-progress-bar warn' in html
    assert 'danger' not in html
    assert '-0,00' not in html


def test_gauge_one_cent_over_is_danger(app):
    html = _card(app, 37.98, 37.97)
    assert 'cat-progress-bar danger' in html
    assert '-0,01' in html
