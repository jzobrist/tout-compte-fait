"""Montants entiers, réglettes + champ, bandeau des virements (« Plan du mois »)."""
import re
from datetime import date

import pytest

from app.models import Category, EnvelopeCeiling
from app.services import ceiling_service as ceil_s
from app.services import contribution_service as cs
from tests.test_ceiling_service import _bm, _cat as _ccat
from tests.test_plan_routes import HX, _account, _cat, _income, _ceiling_url


# --- pure rounding of the shares -------------------------------------------------------------

def test_round_shares_up_to_the_euro():
    assert cs.round_shares({1: 1240.2, 2: 979.01}, {}) == {1: 1241.0, 2: 980.0}


def test_round_shares_zero_stays_zero_and_exact_not_bumped():
    out = cs.round_shares({1: 0.0, 2: 500.0, 3: 100.0000001}, {})
    assert out == {1: 0.0, 2: 500.0, 3: 100.0}


def test_round_shares_never_above_cap():
    out = cs.round_shares({1: 100.3, 2: 200.2}, {1: 100.3, 2: None})
    assert out[1] == 100.3          # capped: min(ceil, cap)
    assert out[2] == 201.0


def test_split_total_covers_need_and_caps_hold():
    bases = {1: 3000.0, 2: 2000.0}
    shares, adjusted, unc = cs.split_with_caps(bases, 2417.77, {1: None, 2: None})
    rounded = cs.round_shares(shares, {})
    assert all(v == int(v) for v in rounded.values())
    assert sum(rounded.values()) >= 2417.77
    shares, adjusted, unc = cs.split_with_caps(bases, 2417.77, {1: None, 2: 500.5})
    rounded = cs.round_shares(shares, {1: None, 2: 500.5})
    assert rounded[2] <= 500.5


# --- report preset / set_ceiling -------------------------------------------------------------

def test_report_preset_floored_to_the_euro(db, household_clean):
    h = household_clean
    pot = h.personal_a
    a = _ccat(db, pot, 'A', pct=0.07213)
    bm = _bm(pot, 1000.0)
    env = ceil_s.report(bm).envelopes[a.id]
    assert env.amount == 72.0 and env.auto


def test_report_preset_floored_both_pots(db, household_clean):
    h = household_clean
    c = _ccat(db, h.common, 'C', pct=0.07213)
    bm = _bm(h.common, 1000.0)
    assert ceil_s.report(bm).envelopes[c.id].amount == 72.0


def test_set_ceiling_stores_whole_euros(db, household_clean):
    h = household_clean
    pot = h.personal_a
    a = _ccat(db, pot, 'A', pct=0.2)
    _bm(pot, 1000.0)
    today = date.today()
    from app.services.budget_service import current_budget_month
    bm = current_budget_month(pot, today)
    ceil_s.set_ceiling(pot, bm.year, bm.month, a, 120.6)
    assert EnvelopeCeiling.query.filter_by(category_id=a.id).one().amount == 121.0


# --- page: slider + field --------------------------------------------------------------------

def _attrs(tag):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', tag))


def _inputs(html, url_part):
    return [t.split('>')[0] for t in html.split('<input') if url_part in t.split('>')[0]]


def test_slider_and_field_are_integers_with_same_bounds(db, household_clean):
    h = household_clean
    _account(db, h.personal_a, 500.0)
    _ccat(db, h.personal_a, 'Loisirs A', pct=1 / 3, floor=10.004)
    _income(h.personal_a, 1000.0)
    html = h.client_a.get('/plan').get_data(as_text=True)
    url = f'/plan/{h.personal_a.id}/ceiling/'
    slider = next(t for t in html.split('<input') if 'type="range"' in t.split('>')[0] and url in t.split('>')[0])
    s = _attrs(slider.split('>')[0])
    assert s['step'] == '1' and 'delay:400ms' not in slider.split('>')[0]
    assert s['hx-trigger'] == 'change'
    for k in ('min', 'max', 'value'):
        assert re.fullmatch(r'\d+', s[k]), (k, s[k])
    assert int(s['min']) == 11            # ceil(10.004)
    field = next(t.split('>')[0] for t in html.split('<input') if 'type="number"' in t.split('>')[0]
                 and url in t.split('>')[0])
    f = _attrs(field)
    assert f['step'] == '1' and f['inputmode'] == 'numeric'
    assert (f['min'], f['max'], f['value']) == (s['min'], s['max'], s['value'])
    assert f['hx-trigger'] == 'change' and f['name'] == 'amount'


def test_slider_row_outside_the_flex_header(db, household_clean):
    h = household_clean
    _ccat(db, h.personal_a, 'Loisirs A', pct=0.3)
    _income(h.personal_a, 1000.0)
    html = h.client_a.get('/plan').get_data(as_text=True)
    i = html.index('type="range"', html.index(f'/plan/{h.personal_a.id}/ceiling/') - 400)
    head = html.rindex('d-flex justify-content-between', 0, i)
    assert 'Clore' in html[head:i]          # the Clore button (end of header row) comes before the slider


def test_envelope_figure_shows_ceiling_without_cents(db, household_clean):
    h = household_clean
    _ccat(db, h.personal_a, 'Loisirs A', pct=0.07213)
    _income(h.personal_a, 1000.0)
    html = h.client_a.get('/plan').get_data(as_text=True)
    fig = re.search(r'id="env-fig-\d+"[^>]*>([^<]*)<', html).group(1)
    assert fig.startswith('72 € ·'), fig


def test_ceiling_post_returns_retained_field_but_no_slider(db, household_clean):
    h = household_clean
    _account(db, h.common, 100.0)
    cat = _cat(db, h.common, 'Culture', pct=0.5)
    _income(h.common, 1000.0)
    r = h.client_a.post(_ceiling_url(h.common, cat), data={'amount': '99999'}, headers=HX)
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert '<input type="range"' not in body and 'type="range"' not in body
    m = re.search(rf'<span[^>]*data-retained-for="{cat.id}"[^>]*>', body)
    assert m and 'hx-swap-oob' in m.group(0)
    stored = EnvelopeCeiling.query.filter_by(category_id=cat.id).one().amount
    assert f'data-value="{int(stored)}"' in m.group(0)
    assert stored == int(stored) and stored < 99999


def test_simulation_shares_are_whole_euros(db, household_clean):
    from tests.test_simulation import TODAY, _env
    from app.services import simulation_service as sim
    h = household_clean
    _env(db, h)
    sim.save_forecast(h.user_a, h.user_a.id, 3001.37)
    sim.save_forecast(h.user_a, h.user_b.id, 2003.11)
    result = sim.simulate(h.common, h.user_a, TODAY)
    assert result.error is None and result.shares
    assert all(v == int(v) for v in result.shares.values()), result.shares
