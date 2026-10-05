"""UBS e-banking CSV export.

Layout: a few "Key:;value;" lines with account info, a blank line, then a
semicolon-separated table. Debits and credits have their own columns. The
merchant name is the first part of Description1; the rest is either the
payment method ("Payment UBS TWINT") or the payee's address.
"""

import csv
import io
import re
from decimal import Decimal

from ..models import Statement, Transaction
from .common import fingerprint, normalize_iban, parse_date, to_decimal

TABLE_HEADER = "Trade date;"
IBAN_IN_TEXT = re.compile(r"IBAN:\s*([A-Z]{2}\d{2}[A-Z0-9 ]+?)\s*(?:;|$)")


def matches(text: str) -> bool:
    return text.startswith("Account number:;") and f"\n{TABLE_HEADER}" in text


def parse(text: str) -> Statement:
    table_start = text.index(f"\n{TABLE_HEADER}") + 1
    info = _read_account_info(text[:table_start])
    statement = Statement(
        bank="ubs",
        account=normalize_iban(info["IBAN"]),
        currency=info["Valued in"],
        opening_balance=to_decimal(info.get("Opening balance")),
        closing_balance=to_decimal(info.get("Closing balance")),
    )

    for row in csv.DictReader(io.StringIO(text[table_start:]), delimiter=";"):
        debit, credit = to_decimal(row["Debit"]), to_decimal(row["Credit"])
        if debit is None and credit is None:
            statement.warnings.append(
                f"Skipped row {row['Transaction no.'] or '?'} without debit or credit "
                "(possibly a detail line of a collective booking)"
            )
            continue

        amount = -abs(debit) if debit is not None else abs(credit)
        name, *rest = [part.strip() for part in row["Description1"].split(";")]
        method = row["Description2"].strip() or (rest[0] if len(rest) == 1 else None)
        trade_date = row["Trade date"] or row["Booking date"]
        statement.transactions.append(
            Transaction(
                date=parse_date(trade_date),
                book_date=parse_date(row["Booking date"]) if row["Booking date"] else None,
                amount=amount,
                currency=row["Currency"],
                counterparty=name,
                description=name,
                external_id=row["Transaction no."]
                or fingerprint("ubs", trade_date, amount, row["Description1"]),
                method=method,
                counterparty_iban=_find_iban(row["Description3"]),
                is_reversal=bool(method) and method.lower().startswith("reversal"),
                notes=row["Description3"].strip(),
            )
        )

    _check_balance(statement)
    return statement


def _read_account_info(text: str) -> dict[str, str]:
    info = {}
    for line in text.splitlines():
        key, _, rest = line.partition(";")
        if key.endswith(":"):
            info[key[:-1]] = rest.split(";")[0].strip()
    return info


def _find_iban(text: str) -> str | None:
    match = IBAN_IN_TEXT.search(text)
    return normalize_iban(match.group(1)) if match else None


def _check_balance(statement: Statement) -> None:
    if statement.opening_balance is None or statement.closing_balance is None:
        return
    total = sum((t.amount for t in statement.transactions), Decimal("0"))
    expected = statement.opening_balance + total
    if expected != statement.closing_balance:
        statement.warnings.append(
            f"Balances don't add up: opening {statement.opening_balance} + transactions "
            f"{total} = {expected}, but the statement says {statement.closing_balance}"
        )
