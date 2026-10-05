from datetime import date
from decimal import Decimal

import pytest

from firefly_uploader.parsers import UnknownFormatError, parse
from firefly_uploader.parsers.common import parse_date, to_decimal


def test_unknown_file_is_rejected():
    with pytest.raises(UnknownFormatError):
        parse(b"Date,Amount\n2026-10-01,12.00\n")


def test_windows_line_endings_are_accepted():
    data = b"Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\r\n"
    data += b"Card Payment,Current,2026-09-02 10:00:00,2026-09-02 10:00:01,Shop,-10.00,0.00,EUR,COMPLETED,90.00\r\n"

    assert parse(data).transactions[0].amount == Decimal("-10.00")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("-2000.00", Decimal("-2000.00")), ("2'000.00", Decimal("2000.00")), ("", None), (None, None)],
)
def test_to_decimal(text, expected):
    assert to_decimal(text) == expected


def test_parse_date_accepts_iso_and_swiss_format():
    assert parse_date("2026-10-05") == parse_date("05.10.2026") == date(2026, 10, 5)
