# Firefly uploader

Web app that reads bank statements (UBS, Revolut) and sends the transactions to the user's
self-hosted Firefly III (v6.7.7) through its API. Python, src layout. The app has its own login;
each app user adds the Firefly users ("connections": URL + token) they send statements to.
Runs on the user's Windows PC during development; will later be hosted on TrueNAS (Docker).

## Commands

- Set up: `python -m venv .venv` then `.venv\Scripts\python -m pip install -e ".[dev]"`
- Tests: `.venv\Scripts\python -m pytest` (the live Firefly test is skipped unless the test instance runs)
- Test Firefly (throwaway, same version, http://localhost:18081):
  `docker compose -f dev/firefly-test/compose.yml up -d`, then `.venv\Scripts\python dev\firefly-test\setup.py`
  (creates the user, writes `FIREFLY_TEST_*` to `.env`, CHF + "UBS"/"Revolut" accounts).
  Reset with `down -v` and run setup again. Ports 7439-8298 are reserved by Windows on this PC.
- Run the web app: `.venv\Scripts\python -m firefly_uploader serve` → http://127.0.0.1:8765
  (data in `uploader.db` or `UPLOADER_DB`; tokens encrypted with `UPLOADER_SECRET_KEY` or the
  `secret.key` file made next to the database). First start asks for a user.
- Forgotten password: `.venv\Scripts\python -m firefly_uploader set-password USERNAME`
- Check `FIREFLY_*` / `FIREFLY_TEST_*` from `.env` (read-only): `... -m firefly_uploader check [--test]`

## Layout

- `src/firefly_uploader/models.py`: bank-independent `Statement` / `Transaction`
- `src/firefly_uploader/parsers/`: one module per bank with `matches(text)` and `parse(text)`;
  `parsers.parse(bytes)` detects the bank and picks the right one
- `src/firefly_uploader/firefly.py`: API client (`FireflyClient.from_env`) and `split_for(tx, ...)`,
  which turns a `Transaction` into a Firefly split
- `src/firefly_uploader/review.py`: review rows (remembered category or transfer, conversion,
  already in Firefly?) and sending
- `src/firefly_uploader/rates.py`: ECB daily rates from frankfurter.dev (asked against EUR, other
  pairs divided out for precision; weekends use the last business day)
- `src/firefly_uploader/store.py`: SQLite: users, sessions, Firefly connections, category rules
  per merchant (per app user), statement → Firefly account links (per Firefly user, since account
  IDs differ between users). Schema changes = a new function appended to `MIGRATIONS`, never an
  edit of an old one: databases upgrade themselves on start.
- `src/firefly_uploader/auth.py`: scrypt passwords, session secrets, token encryption (Fernet)
- `src/firefly_uploader/web.py` + `templates/` + `static/`: FastAPI app, server-rendered forms.
  Every POST form carries `csrf` (the session's token) and is checked with `check_form()`.
- `src/firefly_uploader/static/vendor/`: AdminLTE 4, Bootstrap 5, Bootstrap Icons (MIT), Tom Select
  (Apache-2.0, the searchable category list) and SortableJS (MIT, dragging connections into order),
  fetched by `dev/vendor.py` (pinned versions); never edited by hand. `static/style.css` only adds to them.
- `tests/fixtures/`: made-up statements in the exact format of real exports

## Rules

- The repo is private for now and will be made public: nothing personal in code, tests, docs or
  commits (Firefly URL, names, real amounts, balances, merchants, addresses, IBANs, references).
- Real bank statements live in `samples/` (gitignored). Never commit them. Fixtures copy only the
  format of a real export; all values in them are invented.
- Personal rules (e.g. payments to a broker are transfers to an own investment account, who a
  transfer went to) go in user settings or the database, not in code. Transfer rules store the
  target account's name, so they work for any Firefly user with an account of that name.
- Look: like Firefly III, by using the same MIT building blocks (AdminLTE, Bootstrap, Bootstrap
  Icons) and Firefly's blue (#1e6581). Check pages in headless Edge screenshots (light and dark);
  long native `<select>` lists drew badly in Edge's dark mode, hence Tom Select. Never copy Firefly's own code, templates, CSS or logo: they're
  AGPL-3.0 and this project is MIT.
- Amounts are `Decimal`, signed: negative = money out, fees included.
- Revolut EUR/HUF transactions are converted to CHF (ECB rate of the day) and booked into one
  CHF Firefly account; the original amount goes into Firefly's foreign-amount field. Balances
  needn't match exactly. Generally: whenever statement and account currency differ.
- Firefly users: during development the user sends statements to a separate test user on the real
  instance, which has no real data. Writing as their real Firefly user needs the user's explicit
  OK. Automated tests only use the local throwaway instance or fakes. Pages of a connection show
  which Firefly user it is.
- Firefly tokens never go in code or chat: in the app they're entered in the browser and stored
  encrypted; for `check` they're in `.env` (gitignored). `secret.key` and `*.db` are gitignored.
- Duplicates: every split carries the bank's `external_id`; the review marks rows whose
  `external_id` (or amount within a few days) is already `booked()` in Firefly.
  Firefly also rejects exact copies (`DuplicateTransactionError`) as a safety net.
