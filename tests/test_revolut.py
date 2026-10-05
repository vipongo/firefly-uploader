from datetime import date
from decimal import Decimal
from pathlib import Path

from firefly_uploader.parsers import parse, parse_file

FIXTURE = Path(__file__).parent / "fixtures" / "revolut_eur_sample.csv"
HEADER = "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"


def by_description(statement, description):
    return [t for t in statement.transactions if t.description == description]


def test_reads_account_info():
    statement = parse_file(FIXTURE)

    assert statement.bank == "revolut"
    assert statement.account == "revolut-EUR"
    assert statement.currency == "EUR"
    assert statement.opening_balance == Decimal("500.00")
    assert statement.closing_balance == Decimal("289.51")
    assert statement.warnings == []


def test_skips_reverted_transactions():
    statement = parse_file(FIXTURE)

    assert len(statement.transactions) == 10
    assert by_description(statement, "Example Cloud") == []


def test_card_payment_uses_started_date():
    first_amazon = by_description(parse_file(FIXTURE), "Amazon")[0]

    assert first_amazon.amount == Decimal("-35.90")
    assert first_amazon.date == date(2026, 9, 2)
    assert first_amazon.book_date == date(2026, 9, 8)
    assert first_amazon.method == "Card Payment"


def test_outgoing_transfer_counterparty_drops_to_prefix():
    [t] = by_description(parse_file(FIXTURE), "To Jane Doe")

    assert t.counterparty == "Jane Doe"
    assert t.amount == Decimal("-40.00")


def test_incoming_transfer_from_revolut_user():
    received = by_description(parse_file(FIXTURE), "Revolut Bank UAB")

    assert [t.amount for t in received] == [Decimal("25.00"), Decimal("5.50"), Decimal("30.00")]
    assert all(t.counterparty == "Revolut Bank UAB" for t in received)


def test_ids_are_unique_and_stable():
    first = [t.external_id for t in parse_file(FIXTURE).transactions]
    second = [t.external_id for t in parse_file(FIXTURE).transactions]

    assert len(set(first)) == len(first)
    assert first == second


def test_fee_counts_as_money_out():
    csv = (
        HEADER
        + "Topup,Current,2026-09-01 10:00:00,2026-09-01 10:00:01,Top-up,100.00,0.00,EUR,COMPLETED,100.00\n"
        + "Card Payment,Current,2026-09-02 10:00:00,2026-09-02 10:00:01,Shop,-10.00,0.50,EUR,COMPLETED,89.50\n"
    )

    statement = parse(csv.encode())

    shop = statement.transactions[1]
    assert shop.amount == Decimal("-10.50")
    assert shop.fee == Decimal("0.50")
    assert statement.warnings == []
