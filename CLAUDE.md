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
- Check a connection (read-only): `.venv\Scripts\python -m firefly_uploader check [--test]`

## Layout

- `src/firefly_uploader/models.py`: bank-independent `Statement` / `Transaction`
- `src/firefly_uploader/parsers/`: one module per bank with `matches(text)` and `parse(text)`;
  `parsers.parse(bytes)` detects the bank and picks the right one
- `src/firefly_uploader/firefly.py`: API client (`FireflyClient.from_env`) and `split_for(tx, ...)`,
  which turns a `Transaction` into a Firefly split
- `tests/fixtures/`: made-up statements in the exact format of real exports

## Rules

- The repo is private for now and will be made public: nothing personal in code, tests, docs or
  commits (Firefly URL, names, real amounts, balances, merchants, addresses, IBANs, references).
- Real bank statements live in `samples/` (gitignored). Never commit them. Fixtures copy only the
  format of a real export; all values in them are invented.
- Personal rules (e.g. payments to a broker are transfers to an own investment account, who a
  transfer went to) go in user settings or the database, not in code.
- Amounts are `Decimal`, signed: negative = money out, fees included.
- Revolut EUR/HUF transactions are converted to CHF and booked into one CHF Firefly account;
  the original amount goes into Firefly's foreign-amount field. Balances needn't match exactly.
- Never write to the real Firefly instance without the user's OK; test against a throwaway instance.
- The Firefly token goes in `.env` (gitignored), never in code or chat. See `.env.example`.
- Duplicates: every split carries the bank's `external_id`; check `external_ids()` before uploading.
  Firefly also rejects exact copies (`DuplicateTransactionError`) as a safety net.
