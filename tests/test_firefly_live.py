"""Round trip against the throwaway Firefly from dev/firefly-test, never the real one.

Skipped when it isn't set up or running; see dev/firefly-test/setup.py.
"""

import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import pytest

from firefly_uploader.firefly import (
    DuplicateTransactionError,
    FireflyClient,
    FireflyError,
    FireflyUnreachableError,
    split_for,
)
from firefly_uploader.parsers import parse_file

UBS_FIXTURE = Path(__file__).parent / "fixtures" / "ubs_sample.csv"


@pytest.fixture(scope="module")
def firefly():
    try:
        client = FireflyClient.from_env("FIREFLY_TEST")
    except FireflyError:
        pytest.skip("no test Firefly configured (dev/firefly-test/setup.py)")
    if urlparse(client.url).hostname not in ("localhost", "127.0.0.1"):
        pytest.fail(f"FIREFLY_TEST_URL must be the local throwaway instance, not {client.url}")
    try:
        client.version()
    except FireflyUnreachableError:
        pytest.skip("test Firefly isn't running (docker compose -f dev/firefly-test/compose.yml up -d)")
    yield client
    client.close()


def test_uploads_a_purchase_once(firefly):
    ubs = next(account for account in firefly.asset_accounts() if account.name == "UBS")
    original = next(t for t in parse_file(UBS_FIXTURE).transactions if t.external_id == "1000004GK0000006")
    tx = replace(original, external_id=f"test-{uuid.uuid4().hex}")  # new on every run
    split = split_for(tx, ubs.id, category="Groceries")

    firefly.create_transaction([split])

    booked = firefly.booked(ubs.id, tx.date, tx.date)
    assert [b.amount for b in booked if b.external_id == tx.external_id] == [tx.amount]
    assert "Groceries" in firefly.categories()
    with pytest.raises(DuplicateTransactionError):
        firefly.create_transaction([split])
