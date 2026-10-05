"""What the uploader remembers between uploads, in SQLite: categories per merchant and
which Firefly account each bank account goes to."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
create table if not exists rules (
    counterparty text primary key,  -- see counterparty_key()
    category text,                  -- null when always_ask or a transfer
    always_ask integer not null default 0,
    transfer_account text           -- name of an own Firefly asset account: money moves there
);
-- Account IDs only mean something for one Firefly user, hence firefly_user ("email @ url")
create table if not exists account_links (
    firefly_user text not null,
    statement_account text not null,  -- Statement.account: IBAN or "revolut-EUR"
    firefly_account_id text not null,
    primary key (firefly_user, statement_account)
);
"""


@dataclass
class Rule:
    category: str | None = None
    always_ask: bool = False
    transfer_account: str | None = None  # a name, as account IDs differ between Firefly users


def counterparty_key(name: str) -> str:
    """'SBB EASYRIDE' and 'SBB EasyRide' are the same merchant."""
    return " ".join(name.casefold().split())


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        with self._db() as db:
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("pragma table_info(rules)")}
            if "transfer_account" not in columns:  # databases from before transfers
                db.execute("alter table rules add column transfer_account text")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        # A connection per use: web requests run on several threads.
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db

    def rules(self) -> dict[str, Rule]:
        with self._db() as db:
            rows = db.execute("select counterparty, category, always_ask, transfer_account from rules").fetchall()
        return {key: Rule(category, bool(always_ask), transfer) for key, category, always_ask, transfer in rows}

    def remember(self, counterparty: str, category: str) -> None:
        self._save_rule(counterparty, Rule(category=category))

    def always_ask(self, counterparty: str) -> None:
        self._save_rule(counterparty, Rule(always_ask=True))

    def remember_transfer(self, counterparty: str, account_name: str) -> None:
        self._save_rule(counterparty, Rule(transfer_account=account_name))

    def _save_rule(self, counterparty: str, rule: Rule) -> None:
        with self._db() as db:
            db.execute(
                "insert or replace into rules (counterparty, category, always_ask, transfer_account)"
                " values (?, ?, ?, ?)",
                (counterparty_key(counterparty), rule.category, int(rule.always_ask), rule.transfer_account),
            )

    def linked_account(self, firefly_user: str, statement_account: str) -> str | None:
        with self._db() as db:
            row = db.execute(
                "select firefly_account_id from account_links"
                " where firefly_user = ? and statement_account = ?",
                (firefly_user, statement_account),
            ).fetchone()
        return row[0] if row else None

    def link_account(self, firefly_user: str, statement_account: str, firefly_account_id: str) -> None:
        with self._db() as db:
            db.execute(
                "insert or replace into account_links (firefly_user, statement_account, firefly_account_id)"
                " values (?, ?, ?)",
                (firefly_user, statement_account, firefly_account_id),
            )
