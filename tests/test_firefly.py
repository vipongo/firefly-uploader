"""Firefly client against a fake server (no network)."""

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from firefly_uploader.firefly import (
    DuplicateTransactionError,
    FireflyClient,
    FireflyError,
    FireflyUnreachableError,
    split_for,
)
from firefly_uploader.models import Transaction


def client(handler) -> FireflyClient:
    return FireflyClient("https://firefly.example/", "secret", transport=httpx.MockTransport(handler))


def page(items, current=1, total_pages=1) -> dict:
    return {"data": items, "meta": {"pagination": {"current_page": current, "total_pages": total_pages}}}


def test_sends_token_and_reads_version():
    def handler(request):
        assert request.url == "https://firefly.example/api/v1/about"
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(200, json={"data": {"version": "6.7.7"}})

    assert client(handler).version() == "6.7.7"


def test_follows_pages():
    names = {1: ["Groceries", "Transport"], 2: ["Eating out"]}

    def handler(request):
        number = int(request.url.params["page"])
        items = [{"id": name, "attributes": {"name": name}} for name in names[number]]
        return httpx.Response(200, json=page(items, number, total_pages=2))

    assert client(handler).categories() == ["Eating out", "Groceries", "Transport"]


def test_asset_accounts():
    def handler(request):
        assert request.url.params["type"] == "asset"
        return httpx.Response(200, json=page([{"id": "1", "attributes": {
            "name": "UBS", "currency_code": "CHF", "iban": "", "active": True,
        }}]))

    [account] = client(handler).asset_accounts()
    assert (account.id, account.name, account.currency, account.iban) == ("1", "UBS", "CHF", None)


def firefly_split(**values) -> dict:
    return {"date": "2026-09-03T00:00:00+02:00", "description": "Shop", "external_id": None} | values


def test_booked_transactions_are_signed_from_the_accounts_view():
    def handler(request):
        assert request.url.path == "/api/v1/accounts/7/transactions"
        assert request.url.params["start"] == "2026-09-01"
        assert request.url.params["end"] == "2026-09-30"
        groups = [
            {"id": "1", "attributes": {"transactions": [
                firefly_split(amount="12.500000000000", source_id="7", destination_id="40", external_id="a"),
            ]}},
            {"id": "2", "attributes": {"transactions": [
                firefly_split(amount="100.000000000000", source_id="41", destination_id="7"),
            ]}},
        ]
        return httpx.Response(200, json=page(groups))

    out, back = client(handler).booked("7", date(2026, 9, 1), date(2026, 9, 30))

    assert (out.id, out.date, out.amount, out.external_id) == ("1", date(2026, 9, 3), Decimal("-12.5"), "a")
    assert (back.id, back.amount, back.external_id) == ("2", Decimal("100"), None)


def test_booked_on_a_single_day_asks_for_two():
    def handler(request):
        assert (request.url.params["start"], request.url.params["end"]) == ("2026-09-03", "2026-09-04")
        return httpx.Response(200, json=page([]))

    assert client(handler).booked("7", date(2026, 9, 3), date(2026, 9, 3)) == []


def test_create_transaction_refuses_duplicates():
    def handler(request):
        assert json.loads(request.content)["error_if_duplicate_hash"] is True
        return httpx.Response(422, json={
            "message": "Duplicate of transaction #1.",
            "errors": {"transactions.0.description": ["Duplicate of transaction #1."]},
        })

    with pytest.raises(DuplicateTransactionError):
        client(handler).create_transaction([{"type": "withdrawal"}])


def test_validation_errors_name_the_field():
    def handler(request):
        return httpx.Response(422, json={
            "message": "The value must be more than zero.",
            "errors": {"transactions.0.amount": ["The value must be more than zero."]},
        })

    with pytest.raises(FireflyError, match="transactions.0.amount: The value must be more than zero"):
        client(handler).create_transaction([{"amount": "0"}])


def test_rejected_token():
    def handler(request):
        return httpx.Response(401, json={"message": "Unauthenticated."})

    with pytest.raises(FireflyError, match="rejected the access token"):
        client(handler).version()


def test_html_instead_of_api():
    def handler(request):
        return httpx.Response(302, headers={"Location": "/login"}, text="<html>")

    with pytest.raises(FireflyError, match="unexpected answer"):
        client(handler).version()


def test_unreachable():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(FireflyUnreachableError, match="Can't reach Firefly at https://firefly.example"):
        client(handler).version()


def test_missing_settings():
    with pytest.raises(FireflyError, match="NOT_CONFIGURED_URL"):
        FireflyClient.from_env("NOT_CONFIGURED")


def purchase(**changes) -> Transaction:
    values = dict(
        date=date(2026, 10, 4), amount=Decimal("-42.35"), currency="CHF",
        counterparty="MUSTER BAECKEREI AG", description="MUSTER BAECKEREI AG", external_id="1000004GK0000006",
        book_date=date(2026, 10, 5), notes="Transaction no. 1000004GK0000006",
    )
    return Transaction(**values | changes)


def test_split_for_purchase():
    assert split_for(purchase(), "1", category="Groceries") == {
        "type": "withdrawal",
        "source_id": "1",
        "destination_name": "MUSTER BAECKEREI AG",
        "date": "2026-10-04",
        "book_date": "2026-10-05",
        "amount": "42.35",
        "currency_code": "CHF",
        "description": "MUSTER BAECKEREI AG",
        "external_id": "1000004GK0000006",
        "notes": "Transaction no. 1000004GK0000006",
        "category_name": "Groceries",
    }


def test_split_for_incoming_money():
    split = split_for(purchase(amount=Decimal("6.50"), notes="", book_date=None), "1")

    assert split["type"] == "deposit"
    assert split["source_name"] == "MUSTER BAECKEREI AG"
    assert split["destination_id"] == "1"
    assert split["amount"] == "6.50"
    assert not {"notes", "book_date", "category_name"} & split.keys()
