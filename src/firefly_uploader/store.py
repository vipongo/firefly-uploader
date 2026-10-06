"""What the uploader keeps in SQLite: its users and their login sessions, their Firefly
connections, the category per merchant, and which Firefly account each bank account goes to.

The schema is numbered (pragma user_version) and upgraded on start, so a new version of the app
can run on an old database.
"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .auth import TokenCipher, fingerprint, hash_password, new_secret, verify_password

SESSION_DAYS = 30
EARLY_USER = 0  # rules learned before the app had users; they go to the first user
MAX_NAME_LENGTH = 60


def _v1(db: sqlite3.Connection) -> None:
    """The tables of the first versions, which didn't number the schema."""
    _run(db, """
        create table if not exists rules (
            counterparty text primary key,
            category text,
            always_ask integer not null default 0
        );
        -- Account IDs only mean something for one Firefly user, hence firefly_user ("email @ url")
        create table if not exists account_links (
            firefly_user text not null,
            statement_account text not null,  -- Statement.account: IBAN or "revolut-EUR"
            firefly_account_id text not null,
            primary key (firefly_user, statement_account)
        )
    """)
    if "transfer_account" not in {row[1] for row in db.execute("pragma table_info(rules)")}:
        db.execute("alter table rules add column transfer_account text")


def _v2(db: sqlite3.Connection) -> None:
    """App users, their login sessions and Firefly connections; rules belong to a user."""
    _run(db, f"""
        create table users (
            id integer primary key,
            username text not null unique collate nocase,
            password_hash text not null
        );
        create table sessions (
            secret_hash text primary key,  -- see auth.fingerprint()
            user_id integer not null references users (id) on delete cascade,
            csrf text not null,  -- must come back with every form
            expires_at text not null
        );
        create table connections (
            id integer primary key,
            user_id integer not null references users (id) on delete cascade,
            url text not null,
            firefly_user text not null,  -- email of the Firefly user the token belongs to
            token text not null,  -- encrypted, see auth.TokenCipher
            is_default integer not null default 0,
            unique (user_id, url, firefly_user)
        );
        create table user_rules (
            user_id integer not null,
            counterparty text not null,  -- see counterparty_key()
            category text,  -- null when always_ask or a transfer
            always_ask integer not null default 0,
            transfer_account text,  -- name of an own Firefly asset account: money moves there
            primary key (user_id, counterparty)
        );
        insert into user_rules
            select {EARLY_USER}, counterparty, category, always_ask, transfer_account from rules;
        drop table rules;
        alter table user_rules rename to rules
    """)


def _v3(db: sqlite3.Connection) -> None:
    """Connections get a name of the user's choice and an order; existing ones keep the old order."""
    _run(db, """
        alter table connections add column name text;  -- null: the Firefly user's email is shown
        alter table connections add column position integer not null default 0;
        update connections set position = (
            select count(*) from connections as earlier
            where earlier.user_id = connections.user_id
              and (earlier.url, earlier.firefly_user) < (connections.url, connections.firefly_user)
        )
    """)


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_v1, _v2, _v3]


def _run(db: sqlite3.Connection, script: str) -> None:
    # Not executescript(): that commits, and each migration must apply completely or not at all.
    for statement in script.split(";"):
        if statement.strip():
            db.execute(statement)


@dataclass
class Rule:
    category: str | None = None
    always_ask: bool = False
    transfer_account: str | None = None  # a name, as account IDs differ between Firefly users


@dataclass
class Session:
    user_id: int
    username: str
    csrf: str


@dataclass
class Connection:
    """A Firefly user the app user has a token for."""

    id: int
    url: str
    firefly_user: str
    name: str | None = None
    is_default: bool = False
    token: str | None = None  # None when it can't be decrypted (the secret key changed)

    @property
    def label(self) -> str:
        return self.name or self.firefly_user

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc

    @property
    def key(self) -> str:
        """Identifies the Firefly user for things that only make sense for that user."""
        return f"{self.firefly_user} @ {self.url}"


def counterparty_key(name: str) -> str:
    """'SBB EASYRIDE' and 'SBB EasyRide' are the same merchant."""
    return " ".join(name.casefold().split())


def clean_name(text: str) -> str | None:
    """'  Test   user ' -> 'Test user'; empty -> None."""
    return " ".join(text.split())[:MAX_NAME_LENGTH] or None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | Path, cipher: TokenCipher):
        self.path = Path(path)
        self._cipher = cipher
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            version = db.execute("pragma user_version").fetchone()[0]
            for number, migrate in enumerate(MIGRATIONS[version:], start=version + 1):
                db.execute("begin")
                try:
                    migrate(db)
                    db.execute(f"pragma user_version = {number}")
                    db.execute("commit")
                except BaseException:
                    db.execute("rollback")
                    raise

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        # A connection per use: web requests run on several threads.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("pragma foreign_keys = on")
            yield db

    # Users

    def has_users(self) -> bool:
        with self._db() as db:
            return db.execute("select exists (select 1 from users)").fetchone()[0] == 1

    def create_user(self, username: str, password: str) -> int:
        """The first user also gets the rules learned before there were users."""
        with self._db() as db:
            first = db.execute("select not exists (select 1 from users)").fetchone()[0] == 1
            user_id = db.execute(
                "insert into users (username, password_hash) values (?, ?)", (username, hash_password(password))
            ).lastrowid
            if first:
                db.execute("update rules set user_id = ? where user_id = ?", (user_id, EARLY_USER))
        return user_id

    def check_login(self, username: str, password: str) -> int | None:
        with self._db() as db:
            row = db.execute("select id, password_hash from users where username = ?", (username,)).fetchone()
        if row is None:
            verify_password(password, hash_password("x"))  # as slow as for a real user
            return None
        return row[0] if verify_password(password, row[1]) else None

    def set_password(self, username: str, password: str) -> bool:
        """Also logs the user out everywhere. False if there's no such user."""
        with self._db() as db:
            row = db.execute("select id from users where username = ?", (username,)).fetchone()
            if row is None:
                return False
            db.execute("update users set password_hash = ? where id = ?", (hash_password(password), row[0]))
            db.execute("delete from sessions where user_id = ?", (row[0],))
        return True

    # Login sessions

    def start_session(self, user_id: int) -> str:
        """Returns the secret for the session cookie."""
        secret = new_secret()
        expires_at = (datetime.now(UTC) + timedelta(days=SESSION_DAYS)).isoformat()
        with self._db() as db:
            db.execute("delete from sessions where expires_at < ?", (_now(),))
            db.execute(
                "insert into sessions (secret_hash, user_id, csrf, expires_at) values (?, ?, ?, ?)",
                (fingerprint(secret), user_id, new_secret(), expires_at),
            )
        return secret

    def session(self, secret: str) -> Session | None:
        with self._db() as db:
            row = db.execute(
                "select users.id, users.username, sessions.csrf"
                " from sessions join users on users.id = sessions.user_id"
                " where sessions.secret_hash = ? and sessions.expires_at > ?",
                (fingerprint(secret), _now()),
            ).fetchone()
        return Session(*row) if row else None

    def end_session(self, secret: str) -> None:
        with self._db() as db:
            db.execute("delete from sessions where secret_hash = ?", (fingerprint(secret),))

    # Firefly connections

    def connections(self, user_id: int) -> list[Connection]:
        """Without their tokens, in the user's order."""
        with self._db() as db:
            rows = db.execute(
                "select id, url, firefly_user, name, is_default from connections where user_id = ?"
                " order by position, id",
                (user_id,),
            ).fetchall()
        return [Connection(id, url, user, name, bool(is_default)) for id, url, user, name, is_default in rows]

    def connection(self, user_id: int, connection_id: int) -> Connection | None:
        with self._db() as db:
            row = db.execute(
                "select id, url, firefly_user, name, is_default, token from connections where user_id = ? and id = ?",
                (user_id, connection_id),
            ).fetchone()
        if row is None:
            return None
        id, url, firefly_user, name, is_default, token = row
        return Connection(id, url, firefly_user, name, bool(is_default), self._cipher.decrypt(token))

    def default_connection(self, user_id: int) -> int | None:
        with self._db() as db:
            row = db.execute("select id from connections where user_id = ? and is_default", (user_id,)).fetchone()
        return row[0] if row else None

    def save_connection(self, user_id: int, url: str, firefly_user: str, token: str, name: str | None = None) -> int:
        """New ones go last. Adding the same Firefly user again replaces the token (e.g. when it
        expired), and the name if one is given."""
        with self._db() as db:
            db.execute(
                "insert into connections (user_id, url, firefly_user, token, name, position)"
                " values (?, ?, ?, ?, ?, (select coalesce(max(position) + 1, 0) from connections where user_id = ?))"
                " on conflict (user_id, url, firefly_user)"
                " do update set token = excluded.token, name = coalesce(excluded.name, name)",
                (user_id, url, firefly_user, self._cipher.encrypt(token), name, user_id),
            )
            return db.execute(
                "select id from connections where user_id = ? and url = ? and firefly_user = ?",
                (user_id, url, firefly_user),
            ).fetchone()[0]

    def rename_connection(self, user_id: int, connection_id: int, name: str | None) -> None:
        """None: show the Firefly user's email again."""
        with self._db() as db:
            db.execute("update connections set name = ? where user_id = ? and id = ?", (name, user_id, connection_id))

    def order_connections(self, user_id: int, connection_ids: list[int]) -> None:
        """Puts the connections in this order. Ones not listed (e.g. added meanwhile in another
        tab) keep their order among themselves, after the listed ones."""
        with self._db() as db:
            current = [row[0] for row in db.execute(
                "select id from connections where user_id = ? order by position, id", (user_id,)
            )]
            listed = list(dict.fromkeys(id for id in connection_ids if id in current))
            order = listed + [id for id in current if id not in listed]
            db.executemany(
                "update connections set position = ? where user_id = ? and id = ?",
                [(position, user_id, id) for position, id in enumerate(order)],
            )

    def set_default(self, user_id: int, connection_id: int, is_default: bool = True) -> None:
        """At most one default per user."""
        with self._db() as db:
            if is_default:
                db.execute("update connections set is_default = 0 where user_id = ?", (user_id,))
            db.execute(
                "update connections set is_default = ? where user_id = ? and id = ?",
                (int(is_default), user_id, connection_id),
            )

    def remove_connection(self, user_id: int, connection_id: int) -> None:
        with self._db() as db:
            db.execute("delete from connections where user_id = ? and id = ?", (user_id, connection_id))

    # What to do with a merchant

    def rules(self, user_id: int) -> dict[str, Rule]:
        with self._db() as db:
            rows = db.execute(
                "select counterparty, category, always_ask, transfer_account from rules where user_id = ?",
                (user_id,),
            ).fetchall()
        return {key: Rule(category, bool(always_ask), transfer) for key, category, always_ask, transfer in rows}

    def remember(self, user_id: int, counterparty: str, category: str) -> None:
        self._save_rule(user_id, counterparty, Rule(category=category))

    def always_ask(self, user_id: int, counterparty: str) -> None:
        self._save_rule(user_id, counterparty, Rule(always_ask=True))

    def remember_transfer(self, user_id: int, counterparty: str, account_name: str) -> None:
        self._save_rule(user_id, counterparty, Rule(transfer_account=account_name))

    def _save_rule(self, user_id: int, counterparty: str, rule: Rule) -> None:
        with self._db() as db:
            db.execute(
                "insert or replace into rules (user_id, counterparty, category, always_ask, transfer_account)"
                " values (?, ?, ?, ?, ?)",
                (user_id, counterparty_key(counterparty), rule.category, int(rule.always_ask), rule.transfer_account),
            )

    # Which Firefly account a bank account goes to

    def linked_account(self, firefly_user: str, statement_account: str) -> str | None:
        with self._db() as db:
            row = db.execute(
                "select firefly_account_id from account_links where firefly_user = ? and statement_account = ?",
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
