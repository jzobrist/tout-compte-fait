from app.models import BudgetMonth


def test_perf_profile_prints_locations_and_rolls_back(app, db, household_clean):
    before = BudgetMonth.query.count()
    result = app.test_cli_runner().invoke(args=['perf-profile', '--email', 'a@test.com', '--path', '/plan'])
    assert result.exit_code == 0, result.output
    assert '/plan -> HTTP 200' in result.output
    assert 'services/' in result.output and 'Par emplacement' in result.output
    db.session.expire_all()
    assert BudgetMonth.query.count() == before          # every write rolled back
    assert household_clean.client_a.get('/').status_code == 200   # app still usable afterwards


def test_perf_profile_unknown_user(app, db):
    result = app.test_cli_runner().invoke(args=['perf-profile', '--email', 'nobody@test.com'])
    assert result.exit_code != 0 and 'Utilisateur inconnu' in result.output
