"""Revolut CSV account statement (one file per currency).

Rows are ordered by completion date, and only COMPLETED rows moved money.
Fees are charged on top of the amount, so balance = previous + amount - fee.
There's no transaction number, so we derive a stable one from the row.
"""

import csv
import io
from datetime import datetime
from decimal import Decimal

from ..models import Statement, Transaction
from .common import fingerprint, to_decimal

HEADER = "Type,Product,Started Date,Completed Date,"


def matches(text: str) -> bool:
    return text.startswith(HEADER)


def parse(text: str) -> Statement:
    statement = Statement(bank="revolut", account="", currency="")
    previous_balance = None

    for row in csv.DictReader(io.StringIO(text)):
        if row["State"] != "COMPLETED":
            continue

        amount = to_decimal(row["Amount"])
        fee = to_decimal(row["Fee"]) or Decimal("0")
        total = amount - fee
        balance = to_decimal(row["Balance"])
        currency = row["Currency"]
        description = row["Description"].strip()
        started = datetime.fromisoformat(row["Started Date"])

        if not statement.currency:
            statement.currency = currency
            statement.account = f"revolut-{currency}"
            if balance is not None:
                statement.opening_balance = balance - total
        elif currency != statement.currency:
            statement.warnings.append(
                f"Mixed currencies in one file: {currency} row '{description}' "
                f"in a {statement.currency} statement"
            )
        if previous_balance is not None and balance is not None and previous_balance + total != balance:
            statement.warnings.append(
                f"Balance jump at '{description}' on {started:%Y-%m-%d}: "
                f"{previous_balance} + {total} != {balance}"
            )
        previous_balance = balance

        is_outgoing_transfer = row["Type"] == "Transfer" and amount < 0
        statement.transactions.append(
            Transaction(
                date=started.date(),
                book_date=datetime.fromisoformat(row["Completed Date"]).date(),
                amount=total,
                currency=currency,
                counterparty=description.removeprefix("To ") if is_outgoing_transfer else description,
                description=description,
                external_id="revolut-"
                + fingerprint(currency, row["Started Date"], f"{amount:.2f}", description),
                method=row["Type"],
                fee=fee,
            )
        )

    statement.closing_balance = previous_balance
    return statement
