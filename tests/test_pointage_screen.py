import datetime

import pytest

from app.extensions import db as _db
from app.models import BankAccount, Category
from app.models.operation import Operation

HX = {'HX-Request': 'true'}
TODAY = datetime.date.today()


def _account(pot, name='Courant', bank='Banque Pop'):
    acc = BankAccount(pot_id=pot.id, name=name, bank_name=bank)
    _db.session.add(acc)
    _db.session.commit()
    return acc


def _cat(pot, name):
    cat = Category(name=name, type='fixed_amount', amount=100, pot_id=pot.id)
    _db.session.add(cat)
    _db.session.commit()
    return cat


def _movement(acc, user, ref, label, amount=10.0, day=None, direction='debit', classification=None,
              category=None):
    day = day or TODAY
    op = Operation(pot_id=category.pot_id if category else acc.pot_id, bank_account_id=acc.id,
                   user_id=user.id, amount=amount, date=day, label=label, bank_label=label,
                   bank_date=day, bank_reference=ref, direction=direction,
                   classification=classification, category_id=category.id if category else None)
    _db.session.add(op)
    _db.session.commit()
    return op


def _pending(pot, user, label, amount=10.0, day=None, category=None):
    op = Operation(pot_id=pot.id, user_id=user.id, amount=amount, entered_amount=amount,
                   date=day or TODAY, label=label, direction='debit', classification='category',
                   category_id=category.id if category else None)
    _db.session.add(op)
    _db.session.commit()
    return op


def _get(client, tab=None, headers=None):
    url = '/operations/pointage' + (f'?tab={tab}' if tab else '')
    return client.get(url, headers=headers or {})


def test_tabs_render_with_counts(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Courses')
    _movement(acc, h.user_a, 'R1', 'ACHAT UN')
    _movement(acc, h.user_a, 'R2', 'ACHAT DEUX')
    _pending(h.common, h.user_a, 'EN ATTENTE', category=cat)
    _movement(acc, h.user_a, 'R3', 'DEJA RANGE', classification='category', category=cat)
    html = _get(h.client_a).get_data(as_text=True)
    assert 'ACHAT UN' in html and 'ACHAT DEUX' in html
    assert 'DEJA RANGE' not in html
    assert 'EN ATTENTE' not in html.split('Lier à une saisie')[0]  # only offered inside the link control
    assert 'data-count="a-classer">2<' in html
    assert 'data-count="en-attente">1<' in html
    assert 'data-count="rapproches">1<' in html
    assert 'EN ATTENTE' in _get(h.client_a, 'en-attente').get_data(as_text=True)
    assert 'DEJA RANGE' in _get(h.client_a, 'rapproches').get_data(as_text=True)


def test_suggestion_shown_and_validated_in_one_click(household_ab):
    h = household_ab
    acc = _account(h.common)
    abo = _cat(h.common, 'Abonnements')
    _movement(acc, h.user_a, 'R1', 'NETFLIX 01', classification='category', category=abo)
    second = _movement(acc, h.user_a, 'R2', 'NETFLIX 02')
    html = _get(h.client_a).get_data(as_text=True)
    assert '✨ Abonnements' in html
    resp = h.client_a.post(f'/operations/{second.id}/classify',
                           data={'classification': 'category', 'category_id': abo.id}, headers=HX)
    assert resp.status_code == 200
    op = _db.session.get(Operation, second.id)
    assert op.classification == 'category' and op.category_id == abo.id
    assert 'data-count="a-classer">0<' in resp.get_data(as_text=True)


def test_conflict_row_shows_candidates_and_link_resolves(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Sorties')
    e1 = _pending(h.common, h.user_a, 'RESTO MIDI', 25.0, TODAY - datetime.timedelta(days=1), cat)
    e2 = _pending(h.common, h.user_a, 'RESTO SOIR', 25.0, TODAY - datetime.timedelta(days=2), cat)
    mv = _movement(acc, h.user_a, 'R1', 'CB RESTAURANT', 25.0)
    html = _get(h.client_a).get_data(as_text=True)
    assert 'RESTO MIDI' in html and 'RESTO SOIR' in html and 'Aucune' in html
    assert 'Sorties' in html
    resp = h.client_a.post(f'/operations/{mv.id}/link', data={'entry_id': e1.id}, headers=HX)
    assert resp.status_code == 200
    assert _db.session.get(Operation, mv.id) is None
    merged = _db.session.get(Operation, e1.id)
    assert merged.bank_reference == 'R1'
    assert _db.session.get(Operation, e2.id).bank_reference is None


def test_manual_link_any_amount_and_ineligible_is_400(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Sorties')
    e = _pending(h.common, h.user_a, 'RESTO', 20.0, TODAY - datetime.timedelta(days=40), cat)
    mv = _movement(acc, h.user_a, 'R1', 'CB', 25.0)
    done = _movement(acc, h.user_a, 'R2', 'DEJA', 5.0, classification='off_budget')
    assert h.client_a.post(f'/operations/{done.id}/link', data={'entry_id': e.id}).status_code == 400
    assert h.client_a.post(f'/operations/{mv.id}/link', data={'entry_id': mv.id}).status_code == 400
    assert h.client_a.post(f'/operations/{mv.id}/link', data={'entry_id': e.id}, headers=HX).status_code == 200
    assert _db.session.get(Operation, e.id).amount == 25.0


def test_pending_older_than_10_days_highlighted(household_ab):
    h = household_ab
    cat = _cat(h.common, 'Sorties')
    _pending(h.common, h.user_a, 'VIEILLE', 5, TODAY - datetime.timedelta(days=11), cat)
    _pending(h.common, h.user_a, 'RECENTE', 5, TODAY - datetime.timedelta(days=2), cat)
    html = _get(h.client_a, 'en-attente').get_data(as_text=True)
    assert html.count('pending-old') == 1
    old_row = html.split('VIEILLE')[0].rsplit('<article', 1)[1]
    assert 'pending-old' in old_row


def test_unlink_from_rapproches_tab(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Sorties')
    e = _pending(h.common, h.user_a, 'RESTO', 25.0, TODAY, cat)
    mv = _movement(acc, h.user_a, 'R1', 'CB RESTAURANT', 25.0)
    h.client_a.post(f'/operations/{mv.id}/link', data={'entry_id': e.id})
    resp = h.client_a.post(f'/operations/{e.id}/unlink', data={'tab': 'rapproches'}, headers=HX)
    assert resp.status_code == 200
    assert _db.session.get(Operation, e.id).bank_reference is None
    assert Operation.query.filter_by(bank_reference='R1', classification=None).count() == 1


def test_unlink_non_reconciled_is_400(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Sorties')
    op = _movement(acc, h.user_a, 'R1', 'DIRECT', classification='category', category=cat)
    assert h.client_a.post(f'/operations/{op.id}/unlink').status_code == 400


def test_cannot_delete_bank_movement(household_ab):
    h = household_ab
    acc = _account(h.common)
    mv = _movement(acc, h.user_a, 'R1', 'CB')
    assert h.client_a.post(f'/operations/{mv.id}/delete').status_code == 400
    assert _db.session.get(Operation, mv.id) is not None


def test_pending_can_be_deleted_from_tab(household_ab):
    h = household_ab
    cat = _cat(h.common, 'Sorties')
    e = _pending(h.common, h.user_a, 'A SUPPRIMER', category=cat)
    resp = h.client_a.post(f'/operations/{e.id}/delete', data={'tab': 'en-attente'}, headers=HX)
    assert resp.status_code == 200
    assert _db.session.get(Operation, e.id) is None


def test_classify_invalid_combination_is_400(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Sorties')
    credit = _movement(acc, h.user_a, 'R1', 'VIREMENT', 50, direction='credit')
    debit = _movement(acc, h.user_a, 'R2', 'ACHAT')
    assert h.client_a.post(f'/operations/{credit.id}/classify',
                           data={'classification': 'category', 'category_id': cat.id}).status_code == 400
    assert h.client_a.post(f'/operations/{debit.id}/classify',
                           data={'classification': 'income'}).status_code == 400
    assert h.client_a.post(f'/operations/{debit.id}/classify',
                           data={'classification': 'category'}).status_code == 400
    assert _db.session.get(Operation, debit.id).classification is None


def test_personal_row_offers_common_categories_separately(household_ab):
    h = household_ab
    acc = _account(h.personal_a, 'Carte perso', 'BanquePerso')
    _cat(h.personal_a, 'Loisirs A')
    _cat(h.common, 'Courses commun')
    _movement(acc, h.user_a, 'R1', 'ACHAT')
    h.client_a.post(f'/pots/{h.personal_a.id}/select')
    html = _get(h.client_a).get_data(as_text=True)
    assert 'Loisirs A' in html and 'Courses commun' in html and 'Commun' in html


def test_credit_row_offers_categories_only_for_refund(household_ab):
    h = household_ab
    acc = _account(h.common)
    _cat(h.common, 'Courses')
    _movement(acc, h.user_a, 'R1', 'SALAIRE', 2000, direction='credit')
    html = _get(h.client_a).get_data(as_text=True)
    assert 'Revenu' in html and 'Remboursement' in html


def test_b_never_sees_a_personal_account_name(household_ab):
    h = household_ab
    acc = _account(h.personal_a, 'CompteSecretA', 'BanqueSecreteA')
    cat = _cat(h.common, 'Courses')
    mv = _movement(acc, h.user_a, 'R1', 'ACHAT VISIBLE')
    resp = h.client_a.post(f'/operations/{mv.id}/classify',
                           data={'classification': 'category', 'category_id': cat.id})
    assert resp.status_code in (200, 302)
    assert _db.session.get(Operation, mv.id).pot_id == h.common.id
    pending = _pending(h.common, h.user_a, 'EN ATTENTE VISIBLE', category=cat)
    h.client_b.post(f'/pots/{h.common.id}/select')
    seen_label = False
    for url in ('/operations/pointage?tab=a-classer', '/operations/pointage?tab=en-attente',
                '/operations/pointage?tab=rapproches', '/history'):
        html = h.client_b.get(url).get_data(as_text=True)
        seen_label = seen_label or 'ACHAT VISIBLE' in html
        assert 'CompteSecretA' not in html and 'BanqueSecreteA' not in html, url
    assert seen_label
    # A on the common pot sees it too without the name; A in personal context also fine
    html = h.client_a.get('/operations/pointage?tab=rapproches').get_data(as_text=True)
    assert 'CompteSecretA' in html  # the owner may see his own account


def test_b_cannot_see_a_personal_to_classify_or_pending(household_ab):
    h = household_ab
    acc = _account(h.personal_a, 'CompteSecretA', 'BanqueSecreteA')
    cat = _cat(h.personal_a, 'Perso')
    _movement(acc, h.user_a, 'R1', 'MOUVEMENT SECRET')
    _pending(h.personal_a, h.user_a, 'ATTENTE SECRETE', category=cat)
    for pot in (h.common, h.personal_b):
        h.client_b.post(f'/pots/{pot.id}/select')
        for tab in ('a-classer', 'en-attente', 'rapproches'):
            html = _get(h.client_b, tab).get_data(as_text=True)
            assert 'MOUVEMENT SECRET' not in html and 'ATTENTE SECRETE' not in html


def test_b_cannot_link_or_classify_a_personal_ops(household_ab):
    h = household_ab
    acc = _account(h.personal_a)
    cat_a = _cat(h.personal_a, 'Perso')
    cat_c = _cat(h.common, 'Commun')
    mv = _movement(acc, h.user_a, 'R1', 'MOUVEMENT SECRET', 10)
    e = _pending(h.personal_a, h.user_a, 'ATTENTE SECRETE', 10, category=cat_a)
    own_mv = _movement(_account(h.common), h.user_b, 'R2', 'MIEN', 10)
    own_e = _pending(h.common, h.user_b, 'MIENNE', 10, category=cat_c)
    c = h.client_b
    assert c.post(f'/operations/{mv.id}/classify',
                  data={'classification': 'category', 'category_id': cat_c.id}).status_code == 404
    assert c.post(f'/operations/{mv.id}/link', data={'entry_id': e.id}).status_code == 404
    assert c.post(f'/operations/{own_mv.id}/link', data={'entry_id': e.id}).status_code == 404
    assert c.post(f'/operations/{mv.id}/link', data={'entry_id': own_e.id}).status_code == 404
    assert c.post(f'/operations/{e.id}/unlink').status_code == 404
    assert c.post(f'/operations/{own_mv.id}/classify',
                  data={'classification': 'category', 'category_id': cat_a.id}).status_code == 404
    assert _db.session.get(Operation, mv.id).classification is None


def test_b_never_sees_a_personal_categories_in_reclassify_choices(household_ab):
    h = household_ab
    acc = _account(h.personal_a, 'Carte A', 'BanqueA')
    _cat(h.personal_a, 'Secret A')
    common_cat = _cat(h.common, 'Courses')
    mv = _movement(acc, h.user_a, 'R1', 'ACHAT PARTAGE')
    assert h.client_a.post(f'/operations/{mv.id}/classify',
                           data={'classification': 'category', 'category_id': common_cat.id}).status_code in (200, 302)
    h.client_b.post(f'/pots/{h.common.id}/select')
    for tab in ('a-classer', 'en-attente', 'rapproches'):
        for headers in ({}, HX):
            html = _get(h.client_b, tab, headers).get_data(as_text=True)
            assert 'Secret A' not in html, (tab, headers)
    html = _get(h.client_b, 'rapproches').get_data(as_text=True)
    assert 'ACHAT PARTAGE' in html and 'Courses' in html
    assert 'Secret A' in _get(h.client_a, 'rapproches').get_data(as_text=True)


def test_delete_from_non_pointage_redirects_to_next(household_ab):
    h = household_ab
    cat = _cat(h.common, 'Sorties')
    e = _pending(h.common, h.user_a, 'A SUPPRIMER', category=cat)
    resp = h.client_a.post(f'/operations/{e.id}/delete', data={'next': '/history'}, headers=HX)
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/history')


def test_manual_cross_pot_link_from_a_classer_counts_once(household_ab):
    h = household_ab
    cat = _cat(h.common, 'Sorties')
    e = _pending(h.common, h.user_a, 'RESTO', 20.0, TODAY, cat)
    mv = _movement(_account(h.personal_a), h.user_a, 'R1', 'CB RESTAURANT', 22.0)
    assert _db.session.get(Operation, mv.id).classification is None  # no auto link (amount differs)
    h.client_a.post(f'/pots/{h.personal_a.id}/select')
    html = _get(h.client_a, 'a-classer').get_data(as_text=True)
    assert 'Lier à une saisie' in html and 'RESTO' in html and 'Sorties' in html
    assert h.client_a.post(f'/operations/{mv.id}/link',
                           data={'entry_id': e.id, 'tab': 'a-classer'}, headers=HX).status_code == 200
    ops = Operation.query.filter(Operation.pot_id == h.common.id).all()
    assert [(o.id, o.amount) for o in ops] == [(e.id, 22.0)]
    assert Operation.query.filter_by(bank_reference='R1').count() == 1
    from app.services.budget_service import spent_for_category
    assert spent_for_category(cat, TODAY.year, TODAY.month) == 22.0


def _moved_to_common(h):
    """A's personal-card movement classified on a common category (op now in the common pot)."""
    cat = _cat(h.common, 'Courses')
    mv = _movement(_account(h.personal_a), h.user_a, 'R1', 'ACHAT PARTAGE', 12.0)
    e = _pending(h.common, h.user_a, 'saisie', 12.0, TODAY, cat)
    from app.services.reconcile_service import link
    link(e, mv)
    return e, cat


def test_b_cannot_write_into_a_personal_pot_via_moved_op(household_ab):
    h = household_ab
    op, cat = _moved_to_common(h)
    h.client_b.post(f'/pots/{h.common.id}/select')
    c = h.client_b
    for cls in ('internal', 'off_budget', 'income'):
        assert c.post(f'/operations/{op.id}/classify', data={'classification': cls}).status_code == 400
    assert c.post(f'/operations/{op.id}/unlink').status_code == 404
    html = _get(c, 'rapproches').get_data(as_text=True)
    assert 'ACHAT PARTAGE' in html
    assert 'value="internal"' not in html and 'value="off_budget"' not in html
    assert 'Délier' not in html
    after = _db.session.get(Operation, op.id)
    assert after.pot_id == h.common.id and after.classification == 'category'
    # B may still reclassify onto a common category
    assert c.post(f'/operations/{op.id}/classify',
                  data={'classification': 'category', 'category_id': cat.id}).status_code in (200, 302)


def test_a_can_still_unlink_and_set_internal_on_moved_op(household_ab):
    h = household_ab
    op, _ = _moved_to_common(h)
    html = _get(h.client_a, 'rapproches').get_data(as_text=True)
    assert 'value="internal"' in html and 'Délier' in html
    assert h.client_a.post(f'/operations/{op.id}/classify',
                           data={'classification': 'internal'}).status_code in (200, 302)
    assert _db.session.get(Operation, op.id).pot_id == h.personal_a.id


def _edit_data(cat, amount='99.00', label='nouveau'):
    return {'amount': amount, 'category_id': str(cat.id), 'label': label, 'date': TODAY.isoformat()}


def test_edit_refuses_credit_and_non_category_bank_movements(household_ab):
    h = household_ab
    acc = _account(h.common)
    cat = _cat(h.common, 'Courses')
    credit = _movement(acc, h.user_a, 'R1', 'REMB', 5.0, direction='credit', classification='refund',
                       category=cat)
    internal = _movement(acc, h.user_a, 'R2', 'VIR', 5.0, classification='internal')
    for op in (credit, internal):
        assert h.client_a.post(f'/operations/{op.id}/edit', data=_edit_data(cat)).status_code == 400
        assert _db.session.get(Operation, op.id).amount == 5.0


def test_edit_reconciled_movement_keeps_bank_amount(household_ab):
    h = household_ab
    cat = _cat(h.common, 'Courses')
    other = _cat(h.common, 'Autre')
    acc = _account(h.common)
    e = _pending(h.common, h.user_a, 'saisie', 20.0, TODAY, cat)
    from app.services.reconcile_service import link
    link(e, _movement(acc, h.user_a, 'R1', 'CB', 22.0))
    r = h.client_a.post(f'/operations/{e.id}/edit', data=_edit_data(other, '5.00', 'renommé'))
    assert r.status_code == 302
    op = _db.session.get(Operation, e.id)
    assert (op.amount, op.category_id, op.label) == (22.0, other.id, 'renommé')


def test_income_from_bank_movement_cannot_be_deleted(household_ab):
    h = household_ab
    acc = _account(h.common)
    mv = _movement(acc, h.user_a, 'R1', 'SALAIRE', 100.0, direction='credit')
    assert h.client_a.post(f'/operations/{mv.id}/classify',
                           data={'classification': 'income'}).status_code in (200, 302)
    from app.models.income import Income
    inc = Income.query.filter_by(operation_id=mv.id).one()
    page = h.client_a.get('/incomes').get_data(as_text=True)
    assert f'/incomes/{inc.id}/delete' not in page
    r = h.client_a.post(f'/incomes/{inc.id}/delete', follow_redirects=True)
    assert 'reclassez-le depuis le pointage' in r.get_data(as_text=True)
    assert _db.session.get(Income, inc.id) is not None


def test_pointage_shows_refloat_button_only_on_common_credit(household_ab):
    h = household_ab
    common_acc = _account(h.common, name='Joint')
    _movement(common_acc, h.user_a, 'C1', 'VIR DE B', amount=100.0, direction='credit')
    html = _get(h.client_a).get_data(as_text=True)
    assert 'value="contribution"' in html
    assert 'name="contributor_id"' in html
    _db.session.query(Operation).delete()
    _db.session.commit()
    perso_acc = _account(h.personal_a, name='Perso')
    _movement(perso_acc, h.user_a, 'P1', 'VIR RECU', amount=100.0, direction='credit')
    h.client_a.post(f'/pots/{h.personal_a.id}/select')
    html = _get(h.client_a).get_data(as_text=True)
    assert 'VIR RECU' in html
    assert 'value="contribution"' not in html


def test_classify_refloat_route(household_ab):
    h = household_ab
    acc = _account(h.common, name='Joint')
    op = _movement(acc, h.user_a, 'C1', 'VIR DE B', amount=100.0, direction='credit')
    resp = h.client_a.post(f'/operations/{op.id}/classify', headers=HX,
                           data={'classification': 'contribution', 'contributor_id': h.user_b.id})
    assert resp.status_code == 200
    _db.session.refresh(op)
    assert op.contributor_id == h.user_b.id


def test_pointage_menu_labels_pot(household_ab):
    h = household_ab
    acc = _account(h.personal_a, 'Carte perso', 'BanquePerso')
    _cat(h.personal_a, 'Loisirs')
    _cat(h.common, 'Loisirs')
    _movement(acc, h.user_a, 'R1', 'ACHAT')
    h.client_a.post(f'/pots/{h.personal_a.id}/select')
    html = _get(h.client_a).get_data(as_text=True)
    assert '>Loisirs (Perso)<' in html and '>Loisirs (Commun)<' in html
