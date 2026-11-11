# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

"Tout Compte Fait" (formerly "compta") — a Flask household budgeting web app. French UI/domain vocabulary throughout (see design docs in `docs/superpowers/specs/`). Core use case: a couple with a shared (common) pot plus one private personal pot each; multiple households use the same instance, fully isolated from each other.

## Commands

```bash
# Activate venv (already created at ./venv)
source venv/bin/activate

# Run dev server
FLASK_APP=app flask run
# or
python run.py

# Run all tests
pytest

# Run a single test file / test
pytest tests/test_operation_routes.py
pytest tests/test_operation_routes.py::test_add_operation

# DB migrations (Flask-Migrate/Alembic)
flask db migrate -m "message"
flask db upgrade

# Docker (production-shaped, MariaDB backend)
docker compose up -d --build
```

No lint/format tooling is configured in this repo.

Config selection is via `APP_CONFIG` env var (`development` | `testing` | `production`, see `config.py`); `python run.py` reads `APP_ENV` instead (default `development`). Tests always force `testing` config (SQLite in-memory, CSRF disabled) via `tests/conftest.py`.

## Architecture

Layering is intentional — **routes are kept thin**; business logic lives in `app/services/` so it stays reusable by a future JSON `/api/` for a mobile app (per design doc). When adding a feature, put calculation/decision logic in a service function, not in the route handler.

```
app/
├── models/      SQLAlchemy entities (app/models/__init__.py re-exports all)
├── services/    pure business logic — budget math, alert detection, email sending
├── routes/      Flask blueprints, registered in app/__init__.py:create_app()
├── templates/   Jinja2 + HTMX fragments (files prefixed `_` are HTMX partials)
└── static/      CSS/JS
```

### Data model & budget math (`app/services/budget_service.py`)

- `Household` owns `User`s and `Pot`s. A `Pot` is `kind='common'` (whole household) or `kind='personal'` (visible only to its `owner_id` user); each household gets one common pot and each user one personal pot (`household_service`). `Category`, `Income`, `BudgetMonth`, `Operation`, `AlertLog` and `BankAccount` all carry a NOT NULL `pot_id`; none has a `household_id` (reach it via `pot.household_id`). Only `User` and `Pot` keep `household_id`.
- `BankAccount` belongs to a pot; CSV imports target an account, and `Operation` is unique on (`bank_account_id`, `bank_reference`).
- `Category.type` is one of `fixed_auto` | `fixed_amount` | `dynamic`:
  - `fixed_auto`: « Charge fixe (prélèvement) » — lump sum debited once a month (rent, subscriptions), no carry by default. Nothing is auto-generated any more: the real debit comes from bank sync (`auto_day` column is unused).
  - `fixed_amount`: lump sum, manually recorded.
  - `dynamic`: budget = `percentage` of the pot's disponible income, clamped by `min_amount`/`max_amount`.
- `BudgetMonth` + `BudgetMonthCategory` store the computed allocation per pot/year/month/category (`BudgetMonth` unique on `pot_id`,`year`,`month`). `base_allocated = budget_brut + carry_in`, computed by `recalculate_budget_month()`. **`carry_in` is fixed at `BudgetMonthCategory` creation time and never recomputed on subsequent recalculations** — this is intentional, to avoid double-counting carry-over across repeated recalculations within the same month.
- `BudgetAdjustment` is a manual reallocation between two categories within a month; effective budget for a category = `base_allocated + adjustments_in - adjustments_out` (see `get_category_effective_budget`).
- `income_start_day` on `Pot` (1-28, default 1) controls which calendar month an income "belongs" to for budgeting purposes (`target_budget_month()`) — relevant when salary lands late in the month and should fund the *next* month's budget. Spent is counted over the same budget month window (`budget_month_bounds`: from day `income_start_day` of the previous month). Saving a pot's start day (`income_service.reassign_incomes`) re-attributes its incomes, recomputes carry-ins oldest month first (the one place `carry_in` is rewritten) and deletes remise à flot plans from the first changed month on.

- Category families: `Category.family_id` groups the per-pot exemplars of one category (after_insert hook sets it to the row id when None). `Category.scope` = `shared` (one exemplar in every member's personal pot, later joiners included via `attach_shared_families`) | `own` (only its owner's personal pot) | None (common pot). Name and type are shared within a family; settings (amount, %, min/max, carry, constrained, provision) are per exemplar.
- All family logic lives in `category_service`: `create_category`, `update_family`, `rename_family`, `change_scope`, `change_type`, `delete_family`, `families_for`. `display_name` = Jinja global `category_label`, adding " (Commun)" / " (Perso)".
- Name uniqueness (case-insensitive) is checked per pot, only in pots the actor can access (`check_name_free`). A same-name category in another member's pot is absorbed into the shared family, never an error (that would reveal it).
- Safe deletion (`delete_exemplars`): bank movements go back to « À classer » in their bank account's pot (classification and category cleared); pending manual entries are deleted; `BudgetAdjustment`s removed; `AlertLog.category_id` nulled. `Category.operations` no longer cascades. Other members whose exemplar was deleted get a one-shot `CategoryNotice` banner. The `common_transfer` family is locked (no rename, type, scope or delete).
- Categories page is reached from Paramètres (no navbar entry). One-off dev script: `scripts/dev_categories_scope.py`.

### Operations, pointage & reconciliation

- `Operation` states: **bank movement** (`bank_reference` set; `is_bank_movement`), **pending entry** (`bank_reference` NULL, `classification='category'`; `is_pending`: manual expense awaiting its debit), **to classify** (`bank_reference` set, `classification` NULL; `is_to_classify`). `direction` is `debit`|`credit`; `classification` is `category`|`income`|`refund`|`internal`|`off_budget` (`refund` = credit tied to a category, counted negative in spent; `income` creates the linked `Income` via `Income.operation`). `bank_label`/`bank_date` are the bank's values; `entered_amount` is the amount typed manually before a link (set => the op is a reconciliation, `unlink` can split it).
- `pointage_service`: `classify(op, user, classification, category=None)` (raises `ClassificationError`; a personal-account debit may go to a common category, which moves `op.pot_id` to the common pot), `suggest(op)` (last classification of the same normalized label+direction in the pot).
- `reconcile_service`: window = bank date -10/+3 days and same amount (`candidates_for`); one candidate => auto-link at import, several => conflict row on the pointage screen, manual link allows any amount (`eligible_entries(movement, any_amount=True)`). `link`/`unlink` are single transactions.
- Pointage screen `/operations/pointage?tab=a-classer|en-attente|rapproches` (HTMX, whole panel swapped incl. badges); actions `POST /operations/<id>/classify|link|unlink`. `account_label(op)` (Jinja global) hides a personal account's name/bank from users who cannot access its pot — always use it, never print `op.bank_account` directly. Bank movements cannot be deleted.
- Spent per category is computed by one function (`budget_service.get_spent_for_category`) from classified operations only.

### Alerts (`app/services/alert_service.py`)

Daily APScheduler job (08:00) compares spend pace vs. ideal pace for `dynamic` categories only (fixed categories are lump sums and would always look "overspent" early in the month). Sends at most one email per category per alert type per 7 days (`AlertLog`), via `email_service.send_alert_email`.

### Real balance, free money & remise à flot (J4)

- `balance_service`: `pot_balance(pot)` = Σ `balance_value` (booked = Σ `balance_booked`) of non-archived accounts, unknown if one has no balance; `category_remaining` = 0 if the `BudgetMonthCategory` is closed (`closed_at`) else `max(0, effective − spent)`; `free_money(pot, bm)` = balance − manual pending entries − Σ remaining − supplement still due (personal) + supplements still expected (common). `close_envelope`/`reopen_envelope` only for the pot's current budget month; a closed envelope is not carried over (`_compute_carry_in`).
- `refloat_service`: one `RefloatPlan` per pot/budget month, created by `ensure_plan` once income is received and every balance is fresher than the month start; `deficit = max(0, −free money)` measured with no plan, **frozen** (never recomputed; re-measured next month, so unabsorbed parts come back by themselves). Monthly share = deficit / `months` (1–6). Common pot: `split` sends a part to member supplements (prorata of personal `total_income`, capped by `member_cap` = income − fixed − dynamic floors; uncovered part goes to the envelopes), the rest cuts dynamic envelopes. Cuts: prorata of pct, within capacity `min(remaining, brut − min_amount)`, or manual (`RefloatCut`).
- `recalculate_budget_month` applies them: `pot_disponible` subtracts the owner's supplement on a personal pot, dynamic `base_allocated = dynamic_brut − cut + carry_in`. Repeated recalculation is idempotent.
- Supplements/caps (`member_cap`, personal `pot_disponible` subtraction) apply only to pre-J6 months (those measured by a `RefloatPlan` before J6 started); from J6 on, `contribution_service` replaces them (see below). A transfer received on the common account is classified `contribution` (formerly `refloat_transfer`) with `Operation.contributor_id` at pointage: no `Income`, never in `total_income`.
- « Plan du mois » (`plan` blueprint, `/plan`): sliders post `months`/`split` (`/plan/<pot>/settings`, resets manual cuts) and per-envelope `allocation` (`/plan/<pot>/cut/<cat>`, returns figures only, `hx-swap-oob`, sliders never re-rendered while dragged); Clore/Rouvrir `/plan/<pot>/envelope/<cat>/close|reopen`. The common plan never shows personal figures, only « plafond atteint pour X ».
- Alerts `free_negative` (free < 0) and `free_overdraft` (free < −Σ `overdraft_limit`): `PotAlertState` makes them fire once per crossing; checked at the end of `sync_connection` and in `run_daily_alerts`; banner on dashboard and plan page.

### Contribution au commun à reste à vivre égal (J6, `contribution_service`)

- One `ContributionPlan` per common pot/budget month (`prepare_if_ready`, `target_month`; waits for every member's salary unless `force`, then missing salary = 0). Month matching: common month (y, m) ↔ personal `BudgetMonth` with the same (y, m) (never J4 `matching_personal_month`).
- `member_base(user, y, m)` = personal `total_income` − Σ personal categories `constrained=True` of type `fixed_auto`/`fixed_amount`. Common disponible = Σ bases − Σ common fixed brut; dynamic brut via `dynamic_brut`.
- `need(plan).total` = fixed + dynamic brut (before refloat cuts) + provision `ProvisionAllocation`s + V (`monthly_share × split` of the refloat plan) − surplus S (`max(0, position)`) − common `total_income`. Carry-ins excluded. `shares(plan)` = frozen `ContributionShare`s if validated, else `split(plan)[0]` (`split_with_caps`: water-filling at equal reste à vivre R under the member caps, shares floored at 0, last member takes the rounding remainder); `shortfall` = part uncovered because of caps + Σ `max(0, share − base)` over contributors (`shortfall_from_split`, shared with the simulation).
- Validation: `validate` stores a per-member snapshot (`snapshot` = shares in cents); it counts only while current shares equal it. All members matching -> `status='validated'`, shares frozen in `ContributionShare` (later salary changes ignored), mail via `alert_service.send_plan_validated_alerts` to `alert_recipients(common)` (negative variant has no figures). `reopen` allowed while today < start of month M+1.
- Triggers: `prepare_if_ready` runs from the daily `run_daily_alerts` job, when a salary is classified at pointage (`_prepare_household_plan`) and via `refloat_service.build_view`; `recalculate_household` after any change. `member_status`/`due_for`/`contribution_for` feed the plan page and personal pots (contribution amount, received, due only).
- Position = `free_money(common, M−1)` − net bank movements of the common accounts dated from the start of M (already in the balance). `created_at` = preparation day; `contribution_received` for a J6 month counts transfers from its plan's creation to the next plan's (budget-month window only for non-J6 months). The validate form posts the `snapshot` it displayed: a different current snapshot -> `ContributionError('stale')` -> 409 with the refreshed fragment. On a common J6 month the dashboard « Revenus » = `total_income` + `expected_contributions`, and daily pace alerts run despite `total_income == 0`.
- Solidarité (spec `2026-11-11-simulation-solidarite-design.md`): `member_cap(user, year, month, today, salary_in_balance=None, salary=None)` = `max(0, projection + Σ overdraft_limit des comptes perso non archivés − Household.solidarity_margin)` (marge réglée dans Paramètres → Foyer, `POST /settings/household`, défaut 0); projection = solde perso + salaire du mois M s'il n'est pas encore pointé − charges contraintes restant à passer jusqu'à la fin de M + contribution déjà virée pour M (si son plan existe) − contribution encore due pour le mois commun courant (seulement si ce mois ≠ M) ; solde inconnu ou périmé (`known` faux ou `as_of` avant hier) -> pas de plafond. `measure_caps` (dans la même transaction que la création du plan) fige les plafonds en `ContributionCap` à la préparation du plan ; `split_with_caps` (aussi `split`/`adjusted`) répartit à reste à vivre égal sous plafonds, le reste non couvert va dans `shortfall` ; `ContributionPlan.solidarity_adjusted` -> mention « Montants ajustés par solidarité » (sans nom ni montant). Plan validé : parts figées (`ContributionShare`), plafonds ignorés.
- Simulation (`simulation_service`, bloc `#simulation` de « Plan du mois » commun, `POST /plan/<common>/simulation`) : `simulate(common, user, today)` chaîne le même calcul pour le prochain plan à préparer (`peek_target_month`, lecture seule) avec les salaires saisis par le membre (`SalaryForecast`, visibles de lui seul ; champ vide = suppression, valeur non numérique/NaN/infinie/négative = 400, membre hors foyer = 404). Rien n'est écrit hormis ses `SalaryForecast` ; renvoie `None` seulement si le plan du mois de `peek_target_month` existe déjà (une fois le plan du mois courant créé, il simule le mois suivant). Résultat : « X devrait virer N € », jamais de chiffre perso de l'autre.
- Routes in `plan` blueprint: `/plan/<common>/contribution/force|validate|reopen`, `/plan/<common>/allocation/<cat>` (provision categories only; 404 for non-common pot, 400 otherwise).
- Privacy rule: nothing of a member's personal pot (salary, base, constrained charges and names, R, personal free money) appears in any page, fragment, e-mail, flash or HTML attribute; only each member's contribution, received and due. Guarded by `tests/test_privacy.py` (`test_common_plan_never_shows_partner_salary`, `test_validation_mail_has_no_salary`, `test_plan_page_never_shows_partner_cap_or_balance`, `test_simulation_never_shows_partner_figures`).

### Bank sync (Enable Banking, `bank_sync_service`)

- One `BankConnection` per user (own Enable Banking app id + RSA key, encrypted at rest with Fernet key env `BANK_SECRETS_KEY`; `secrets_service`). `BankAccountLink` maps a provider account to a `BankAccount` (new account, or attach to an existing accessible account without fingerprint; a known fingerprint always wins). A joint account may be linked by several members: only the `is_primary` link syncs, and the lowest-id non-orphaned non-primary link takes over only when the primary's connection is `expired`/`setup` (a primary in `error` is not replaced).
- Connection statuses: `active` and `error` are synced (`error` is retried by the daily job and the manual button); `expired` needs a reconnection (no sync); `setup` has no session yet. On reconnection, links are matched by provider uid, then by IBAN fingerprint (new uid is stored on the same link); links missing from the new session are `orphaned` (not synced, primary handed to the next eligible link, pending movements purged when no live link remains).
- `sync_connection` imports BOOK transactions via `import_service.ingest_movements` (dedupe by reference, auto-reconcile), replaces `PendingBankMovement` (PDNG, shown collapsed in pointage « À classer », not classifiable), stores balances. Sync window is per link (`BankAccountLink.last_synced_at` - 5 days); a link never synced starts the day after the latest `bank_date` of its account's existing bank operations (e.g. CSV history), else 90 days back. `BankConnection.last_sync_at` is informational. Auth error => status `expired`; other errors => `error` with a short generic `last_error` (never the exception text). Provider references longer than 50 chars are stored as `h:`+sha256.
- Always get a client through `bank_sync_service.get_client(connection)`; tests monkeypatch it with a fake client (see `tests/test_bank_sync.py`) — never call the real API in tests.
- `APP_BASE_URL` (optional) builds the link in expiry e-mails sent from scheduler jobs. Banner and « Synchroniser maintenant » only concern the viewer's own connection.

### HTMX pattern

Routes check `request.headers.get('HX-Request')` to decide between returning a full page or a template fragment (e.g. `dashboard/_category_list.html`) for in-place swaps. When adding an operation via HTMX, the route recomputes the budget month and re-renders the category list fragment rather than redirecting.

### Auth & multi-tenancy

Flask-Login based. Access rules live in `app/services/access_service.py` (`accessible_pots`, `can_access_pot`, `get_current_pot`, `pot_from_form_or_404`, `load_pot_object_or_404`):

- An inaccessible pot or pot-scoped object (other household, or someone else's personal pot) returns **404, never 403**, so existence is not leaked.
- The session's `current_pot_id` (set by `POST /pots/<id>/select`) only chooses what is *displayed*. **The pot of a write is never taken from the session alone**: derive it from the chosen object (category/account -> its pot) or from a hidden `pot_id` form field validated with `can_access_pot`.
- Budget and `income_start_day` are per pot; the dashboard shows the current pot only.

`Household.generate_invite()`/`is_invite_valid()` handle time-limited invite-code signup links; a joining user gets a personal pot.

Production: `/register` is closed unless `ALLOW_REGISTRATION=true` (`auth.registration_open`; the first account of an empty DB is always allowed, invite links always work, closed = 404 and no link on the login page). `PROXY_FIX` (production) wraps the app in Werkzeug `ProxyFix` (one proxy, `X-Forwarded-For`/`-Proto`) so `_external` URLs are https; session/remember cookies are `Secure`.

### Migrations

Single squashed `migrations/versions/*_initial_schema.py`; name constraints explicitly (SQLite-portable). The pre-squash dev DB was backed up as `instance/tcf_dev.db.bak` (starting from an empty DB was approved).

### Scheduler jobs

Registered in `create_app()`, skipped when `app.testing` is true:
- `run_daily_alerts` — cron, daily 08:00
- `bank_sync_service.sync_all` — cron, daily 06:00 (every `active` or `error` bank connection)
- `bank_sync_service.check_expiries` — cron, daily 07:00 (e-mail 14 days before consent expiry)

Jobs run in-process: deploy with a **single gunicorn worker**, otherwise each worker would sync and e-mail.

## Design docs

`docs/superpowers/specs/` and `docs/superpowers/plans/` (kept locally, git-ignored — not published) contain the original design/implementation docs (in French) for major features — check these before making non-trivial changes to budget math, UI/UX conventions, or deployment, since they capture the "why" behind decisions like the carry_in freeze behavior above.
