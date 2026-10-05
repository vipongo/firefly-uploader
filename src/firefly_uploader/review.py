"""Prepare a statement for review and send the rows the user kept to Firefly."""

from dataclasses import dataclass, replace
from datetime import date, timedelta

from .firefly import (
    Account,
    Booked,
    DuplicateTransactionError,
    FireflyClient,
    FireflyError,
    FireflyUnreachableError,
    split_for,
)
from .models import Statement, Transaction
from .parsers.common import normalize_iban
from .store import Rule, Store, counterparty_key

DAYS_APART = 3  # a transaction typed in by hand may carry a slightly different date

# What happens to the chosen category next time this merchant shows up
REMEMBER, ONCE, ALWAYS_ASK = "remember", "once", "always_ask"

# Outcome of sending a row
CREATED, SKIPPED, DUPLICATE, FAILED = "created", "skipped", "duplicate", "failed"


@dataclass
class Row:
    tx: Transaction
    counterparty: str = ""  # as sent to Firefly; the statement's name unless changed in the review
    category: str = ""
    remember: str = REMEMBER
    from_rule: bool = False  # category filled in from a remembered merchant
    in_firefly: Booked | None = None  # looks like this transaction is already in Firefly...
    same_id: bool = False  # ...because it was uploaded before (else: same amount, close date)
    include: bool = True

    def __post_init__(self) -> None:
        self.counterparty = self.counterparty or self.tx.counterparty

    def as_sent(self) -> Transaction:
        """The transaction with the name chosen in the review; the statement's name goes in the notes."""
        tx = self.tx
        if self.counterparty == tx.counterparty:
            return tx
        description = self.counterparty if tx.description == tx.counterparty else tx.description
        notes = "\n".join(filter(None, [f"Name in the statement: {tx.counterparty}", tx.notes]))
        return replace(tx, counterparty=self.counterparty, description=description, notes=notes)


@dataclass
class Outcome:
    row: Row
    status: str
    message: str = ""
    firefly_id: str | None = None


def pick_account(statement: Statement, accounts: list[Account], linked_id: str | None) -> Account | None:
    """The account used last time, else one with the statement's IBAN or the bank's name."""
    for account in accounts:
        if account.id == linked_id:
            return account
    for account in accounts:
        if account.iban and normalize_iban(account.iban) == statement.account:
            return account
    for account in accounts:
        if statement.bank in account.name.casefold():
            return account
    return None


def search_range(statement: Statement) -> tuple[date, date]:
    dates = [tx.date for tx in statement.transactions]
    return min(dates) - timedelta(days=DAYS_APART), max(dates) + timedelta(days=DAYS_APART)


def prepare(statement: Statement, booked: list[Booked], rules: dict[str, Rule]) -> list[Row]:
    rows = [_row(tx, rules.get(counterparty_key(tx.counterparty))) for tx in statement.transactions]
    _find_in_firefly(rows, booked)
    for row in rows:
        row.include = row.in_firefly is None
    return rows


def _row(tx: Transaction, rule: Rule | None) -> Row:
    if rule is None:
        return Row(tx)
    if rule.always_ask:
        return Row(tx, remember=ALWAYS_ASK)
    return Row(tx, category=rule.category or "", from_rule=True)


def _find_in_firefly(rows: list[Row], booked: list[Booked]) -> None:
    """Match rows to Firefly transactions, each Firefly transaction to one row at most."""
    unclaimed = list(booked)
    for row in rows:
        match = next((b for b in unclaimed if b.external_id == row.tx.external_id), None)
        if match:
            row.in_firefly, row.same_id = match, True
            unclaimed.remove(match)
    # Same amount, closest dates first: so a Firefly transaction of the 5th goes to the row of
    # the 5th, not to a row of the 4th with the same amount that happens to come first.
    pairs = sorted(
        (abs((b.date - row.tx.date).days), row_number, booked_number)
        for row_number, row in enumerate(rows) if row.in_firefly is None
        for booked_number, b in enumerate(unclaimed)
        if b.amount == row.tx.amount and abs((b.date - row.tx.date).days) <= DAYS_APART
    )
    matched_rows, matched_booked = set(), set()
    for _, row_number, booked_number in pairs:
        if row_number not in matched_rows and booked_number not in matched_booked:
            rows[row_number].in_firefly = unclaimed[booked_number]
            matched_rows.add(row_number)
            matched_booked.add(booked_number)


def save_rules(rows: list[Row], store: Store) -> None:
    for row in rows:
        if not row.include:
            continue
        if row.remember == ALWAYS_ASK:
            store.always_ask(row.counterparty)
        elif row.remember == REMEMBER and row.category:
            store.remember(row.counterparty, row.category)


def send(rows: list[Row], account_id: str, firefly: FireflyClient) -> list[Outcome]:
    outcomes = []
    for number, row in enumerate(rows):
        if not row.include:
            outcomes.append(Outcome(row, SKIPPED))
            continue
        try:
            firefly_id = firefly.create_transaction([split_for(row.as_sent(), account_id, row.category or None)])
        except DuplicateTransactionError as error:
            outcomes.append(Outcome(row, DUPLICATE, str(error)))
        except FireflyUnreachableError as error:
            outcomes.append(Outcome(row, FAILED, str(error)))
            for rest in rows[number + 1:]:
                if rest.include:
                    outcomes.append(Outcome(rest, FAILED, "Not sent: Firefly stopped answering"))
                else:
                    outcomes.append(Outcome(rest, SKIPPED))
            break
        except FireflyError as error:
            outcomes.append(Outcome(row, FAILED, str(error)))
        else:
            outcomes.append(Outcome(row, CREATED, firefly_id=firefly_id))
    return outcomes
