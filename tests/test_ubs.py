from datetime import date
from decimal import Decimal
from pathlib import Path

from firefly_uploader.parsers import parse, parse_file

FIXTURE = Path(__file__).parent / "fixtures" / "ubs_sample.csv"


def by_id(statement, external_id):
    return next(t for t in statement.transactions if t.external_id == external_id)


def test_reads_account_info():
    statement = parse_file(FIXTURE)

    assert statement.bank == "ubs"
    assert statement.account == "CH1234567890123456789"
    assert statement.currency == "CHF"
    assert statement.opening_balance == Decimal("2500.00")
    assert statement.closing_balance == Decimal("1918.85")
    assert len(statement.transactions) == 8
    assert statement.warnings == []


def test_twint_payment():
    t = by_id(parse_file(FIXTURE), "1000005GK0000008")

    assert t.amount == Decimal("-6.50")
    assert t.currency == "CHF"
    assert t.counterparty == "SBB EASYRIDE"
    assert t.method == "Payment UBS TWINT"
    assert t.date == date(2026, 10, 5)
    assert not t.is_reversal


def test_twint_reversal_is_flagged():
    t = by_id(parse_file(FIXTURE), "1000005GK0000007")

    assert t.amount == Decimal("6.50")
    assert t.counterparty == "SBB EasyRide"
    assert t.method == "Reversal UBS TWINT"
    assert t.is_reversal


def test_uses_trade_date_and_keeps_booking_date():
    t = by_id(parse_file(FIXTURE), "1000004GK0000006")

    assert t.counterparty == "MUSTER BAECKEREI AG"
    assert t.date == date(2026, 10, 4)
    assert t.book_date == date(2026, 10, 5)


def test_payment_order_extracts_payee_and_iban():
    t = by_id(parse_file(FIXTURE), "1000001TO0000002")

    assert t.amount == Decimal("-500.00")
    assert t.counterparty == "Example Broker Ltd."
    assert t.counterparty_iban == "LI9876543210987654321"
    assert t.method == "e-banking payment order"


def test_warns_when_balances_dont_add_up():
    data = FIXTURE.read_bytes().replace(b"Closing balance:;1918.85;", b"Closing balance:;1900.00;")

    statement = parse(data)

    assert len(statement.warnings) == 1
    assert "don't add up" in statement.warnings[0]


def test_skips_row_without_amount_with_warning():
    detail_row = b"2026-10-05;;2026-10-05;2026-10-05;CHF;;;-10.00;;X1;Detail;;;;\n"

    statement = parse(FIXTURE.read_bytes().rstrip(b"\r\n") + b"\n" + detail_row)

    assert len(statement.transactions) == 8
    assert statement.warnings == [
        "Skipped row X1 without debit or credit (possibly a detail line of a collective booking)"
    ]
