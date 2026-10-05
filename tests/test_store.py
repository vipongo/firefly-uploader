import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from firefly_uploader.auth import TokenCipher, hash_password, load_key, verify_password
from firefly_uploader.store import EARLY_USER, MIGRATIONS, Rule, Store

KEY = TokenCipher.new_key()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "uploader.db", TokenCipher(KEY))


def test_passwords():
    stored = hash_password("correct horse")

    assert stored.startswith("scrypt$") and "correct horse" not in stored
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong horse", stored)
    assert not verify_password("correct horse", "garbage")


def test_login(store):
    assert not store.has_users()
    user_id = store.create_user("alice", "a long password")

    assert store.has_users()
    assert store.check_login("alice", "a long password") == user_id
    assert store.check_login("ALICE", "a long password") == user_id  # usernames ignore case
    assert store.check_login("alice", "wrong") is None
    assert store.check_login("bob", "a long password") is None


def test_sessions(store):
    user_id = store.create_user("alice", "a long password")
    secret = store.start_session(user_id)

    session = store.session(secret)

    assert (session.user_id, session.username) == (user_id, "alice")
    assert len(session.csrf) > 30
    assert store.session("made up") is None
    store.end_session(secret)
    assert store.session(secret) is None


def test_new_password_logs_out_everywhere(store):
    user_id = store.create_user("alice", "a long password")
    secret = store.start_session(user_id)

    assert store.set_password("alice", "another long password")

    assert store.session(secret) is None
    assert store.check_login("alice", "another long password") == user_id
    assert not store.set_password("bob", "whatever password")


def test_tokens_are_stored_encrypted(store):
    user_id = store.create_user("alice", "a long password")

    connection_id = store.save_connection(user_id, "https://firefly.example", "me@example.com", "secret-token")

    with closing(sqlite3.connect(store.path)) as db:
        [stored] = db.execute("select token from connections").fetchone()
    assert "secret-token" not in stored
    assert store.connection(user_id, connection_id).token == "secret-token"


def test_token_unreadable_with_another_key(store):
    user_id = store.create_user("alice", "a long password")
    connection_id = store.save_connection(user_id, "https://firefly.example", "me@example.com", "secret-token")

    other_key = Store(store.path, TokenCipher(TokenCipher.new_key()))

    assert other_key.connection(user_id, connection_id).token is None


def test_adding_the_same_firefly_user_replaces_the_token(store):
    user_id = store.create_user("alice", "a long password")
    first = store.save_connection(user_id, "https://firefly.example", "me@example.com", "old")

    second = store.save_connection(user_id, "https://firefly.example", "me@example.com", "new")

    assert first == second
    assert store.connection(user_id, first).token == "new"


def test_one_default_connection(store):
    user_id = store.create_user("alice", "a long password")
    test = store.save_connection(user_id, "https://firefly.example", "test@example.com", "t1")
    real = store.save_connection(user_id, "https://firefly.example", "real@example.com", "t2")

    store.set_default(user_id, test)
    store.set_default(user_id, real)

    assert store.default_connection(user_id) == real
    assert [c.is_default for c in store.connections(user_id)] == [True, False]  # real, test
    store.set_default(user_id, real, is_default=False)
    assert store.default_connection(user_id) is None


def test_connections_belong_to_their_user(store):
    alice = store.create_user("alice", "a long password")
    bob = store.create_user("bob", "another password")
    connection_id = store.save_connection(alice, "https://firefly.example", "me@example.com", "token")

    assert store.connection(bob, connection_id) is None
    assert store.connections(bob) == []
    store.remove_connection(bob, connection_id)
    assert store.connection(alice, connection_id) is not None


def test_rules_ignore_case_and_spacing(store):
    user_id = store.create_user("alice", "a long password")
    store.remember(user_id, "SBB  EasyRide", "Public Transport")
    store.remember(user_id, "sbb easyride", "Travel Home")

    assert store.rules(user_id) == {"sbb easyride": Rule("Travel Home")}


def test_rules_belong_to_their_user(store):
    alice = store.create_user("alice", "a long password")
    bob = store.create_user("bob", "another password")
    store.remember(alice, "SBB", "Public Transport")

    assert store.rules(bob) == {}


def test_account_links_belong_to_one_firefly_user(store):
    store.link_account("test@example.com @ https://firefly.example", "CH12", "12")

    assert store.linked_account("test@example.com @ https://firefly.example", "CH12") == "12"
    assert store.linked_account("real@example.com @ https://firefly.example", "CH12") is None


def test_database_from_before_users_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as db, db:  # as the first versions left it
        db.execute("create table rules (counterparty text primary key, category text, always_ask integer not null default 0)")
        db.execute("insert into rules values ('sbb easyride', 'Public Transport', 0)")

    store = Store(path, TokenCipher(KEY))

    with closing(sqlite3.connect(path)) as db:
        assert db.execute("pragma user_version").fetchone()[0] == len(MIGRATIONS)
    assert store.rules(EARLY_USER) == {"sbb easyride": Rule("Public Transport")}
    first = store.create_user("alice", "a long password")
    assert store.rules(first) == {"sbb easyride": Rule("Public Transport")}
    assert store.rules(store.create_user("bob", "another password")) == {}


def test_upgrading_twice_changes_nothing(tmp_path):
    Store(tmp_path / "uploader.db", TokenCipher(KEY)).create_user("alice", "a long password")

    assert Store(tmp_path / "uploader.db", TokenCipher(KEY)).has_users()


def test_key_from_settings_or_file(tmp_path):
    key_file = tmp_path / "secret.key"

    made = load_key(None, key_file)

    assert key_file.read_text().strip() == made
    assert load_key(None, key_file) == made
    assert load_key("configured", key_file) == "configured"
