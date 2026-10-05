# Firefly uploader

Web app that reads bank statements (UBS, Revolut) and sends the transactions to the user's
self-hosted Firefly III (v6.7.7; URL in `.env`) through its API. Python, src layout.
Runs on the user's Windows PC during development; will later be hosted on TrueNAS (Docker).

## Commands

- Set up: `python -m venv .venv` then `.venv\Scripts\python -m pip install -e ".[dev]"`
- Tests: `.venv\Scripts\python -m pytest` (the live Firefly test is skipped unless the test instance runs)
- Test Firefly (throwaway, same version, http://localhost:18081):
  `docker compose -f dev/firefly-test/compose.yml up -d`, then `.venv\Scripts\python dev\firefly-test\setup.py`
  (creates the user, writes `FIREFLY_TEST_*` to `.env`, CHF + "UBS"/"Revolut" accounts).
  Reset with `down -v` and run setup again. Ports 7439-8298 are reserved by Windows on this PC.
- Run the web app: `.venv\Scripts\python -m firefly_uploader serve [--test]` → http://127.0.0.1:8765
  (remembered answers in `uploader.db`, or `UPLOADER_DB`)
- Check a connection (read-only): `.venv\Scripts\python -m firefly_uploader check [--test]`

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
- `src/firefly_uploader/store.py`: SQLite: category rules per merchant, statement → Firefly account
  links (per Firefly user, since account IDs differ between users)
- `src/firefly_uploader/web.py` + `templates/` + `static/`: FastAPI app, server-rendered forms
- `tests/fixtures/`: made-up statements in the exact format of real exports

## Rules

- The repo is private for now and will be made public: nothing personal in code, tests, docs or
  commits (Firefly URL, names, real amounts, balances, merchants, addresses, IBANs, references).
- Real bank statements live in `samples/` (gitignored). Never commit them. Fixtures copy only the
  format of a real export; all values in them are invented.
- Personal rules (e.g. payments to a broker are transfers to an own investment account, who a
  transfer went to) go in user settings or the database, not in code. Transfer rules store the
  target account's name, so they work for any Firefly user with an account of that name.
- Amounts are `Decimal`, signed: negative = money out, fees included.
- Revolut EUR/HUF transactions are converted to CHF (ECB rate of the day) and booked into one
  CHF Firefly account; the original amount goes into Firefly's foreign-amount field. Balances
  needn't match exactly. Generally: whenever statement and account currency differ.
- Firefly users: `.env` currently holds a token for a separate test user on the real instance, which
  has no real data; uploads during development go there. Writing as the real user (by swapping the
  token) needs the user's explicit OK. Automated tests only use the local throwaway instance.
  `check` prints which user a token belongs to; the web app must show it too.
- The Firefly token goes in `.env` (gitignored), never in code or chat. See `.env.example`.
- Duplicates: every split carries the bank's `external_id`; the review marks rows whose
  `external_id` (or amount within a few days) is already `booked()` in Firefly.
  Firefly also rejects exact copies (`DuplicateTransactionError`) as a safety net.
