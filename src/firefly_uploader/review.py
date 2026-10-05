"""Prepare a statement for review and send the rows the user kept to Firefly."""

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from .firefly import (
    Account,
    Booked,
    DuplicateTransactionError,
    FireflyClient,
    FireflyError,
    FireflyUnreachableError,
    split_for,
)
from .models import Conversion, Statement, Transaction
from .parsers.common import normalize_iban
from .rates import DailyRates
from .store import Rule, Store, counterparty_key

DAYS_APART = 3  # a transaction typed in by hand may carry a slightly different date
RATE_TOLERANCE = Decimal("0.03")  # ...and, when converted by hand, another day's or bank's rate

# What happens to the chosen category next time this merchant shows up
REMEMBER, ONCE, ALWAYS_ASK = "remember", "once", "always_ask"

# In the category list, own accounts appear as "transfer:<account id>"
TRANSFER = "transfer:"

# Outcome of sending a row
CREATED, SKIPPED, DUPLICATE, FAILED = "created", "skipped", "duplicate", "failed"


@dataclass
class Row:
    tx: Transaction
    counterparty: str = ""  # as sent to Firefly; the statement's name unless changed in the review
    category: str = ""
    transfer_with: Account | None = None  # own account the money moves to or from, instead
    remember: str = REMEMBER
    from_rule: bool = False  # category or transfer filled in from a remembered merchant
    conversion: Conversion | None = None  # when the account's currency isn't the statement's
    in_firefly: Booked | None = None  # looks like this transaction is already in Firefly...
    same_id: bool = False  # ...because it was uploaded before (else: similar amount, close date)
    include: bool = True

    def __post_init__(self) -> None:
        self.counterparty = self.counterparty or self.tx.counterparty

    @property
    def amount(self) -> Decimal:
        """As booked on the Firefly account."""
        return self.conversion.amount if self.conversion else self.tx.amount

    @property
    def choice(self) -> str:
        """The value of the row's category list: a category or TRANSFER + account ID."""
        return TRANSFER + self.transfer_with.id if self.transfer_with else self.category

    def choose(self, value: str, own_accounts: list[Account]) -> None:
        """Set the category or transfer from a value of the category list."""
        self.category, self.transfer_with = value, None
        if value.startswith(TRANSFER):
            account_id = value.removeprefix(TRANSFER)
            self.category = ""
            self.transfer_with = next((a for a in own_accounts if a.id == account_id), None)

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


def choice_for(rule: Rule, own_accounts: list[Account]) -> str:
    """What a remembered rule selects in the category list ("" if its account is gone)."""
    if rule.transfer_account:
        name = rule.transfer_account.casefold()
        account = next((a for a in own_accounts if a.name.casefold() == name), None)
        return TRANSFER + account.id if account else ""
    return rule.category or ""


def prepare(
    statement: Statement,
    booked: list[Booked],
    rules: dict[str, Rule],
    *,
    rates: DailyRates | None = None,
    own_accounts: list[Account] = (),
) -> list[Row]:
    """Rows for the review. `rates` converts into the Firefly account's currency; `own_accounts`
    are the other asset accounts, which money can be transferred to or from."""
    rows = []
    for tx in statement.transactions:
        row = Row(tx, conversion=rates.convert(tx.amount, tx.date) if rates else None)
        rule = rules.get(counterparty_key(tx.counterparty))
        if rule and rule.always_ask:
            row.remember = ALWAYS_ASK
        elif rule and (choice := choice_for(rule, own_accounts)):
            row.choose(choice, own_accounts)
            row.from_rule = True
        rows.append(row)
    _find_in_firefly(rows, booked)
    for row in rows:
        row.include = row.in_firefly is None
    return rows


def _find_in_firefly(rows: list[Row], booked: list[Booked]) -> None:
    """Match rows to Firefly transactions, each Firefly transaction to one row at most."""
    unclaimed = list(booked)
    for row in rows:
        match = next((b for b in unclaimed if b.external_id == row.tx.external_id), None)
        if match:
            row.in_firefly, row.same_id = match, True
            unclaimed.remove(match)
    # Closest dates and amounts first: so a Firefly transaction of the 5th goes to the row of
    # the 5th, not to a row of the 4th with the same amount that happens to come first.
    pairs = sorted(
        (abs((b.date - row.tx.date).days), abs(b.amount - row.amount), row_number, booked_number)
        for row_number, row in enumerate(rows) if row.in_firefly is None
        for booked_number, b in enumerate(unclaimed)
        if abs((b.date - row.tx.date).days) <= DAYS_APART and _same_money(row, b)
    )
    matched_rows, matched_booked = set(), set()
    for _, _, row_number, booked_number in pairs:
        if row_number not in matched_rows and booked_number not in matched_booked:
            rows[row_number].in_firefly = unclaimed[booked_number]
            matched_rows.add(row_number)
            matched_booked.add(booked_number)


def _same_money(row: Row, booked: Booked) -> bool:
    if booked.foreign_currency == row.tx.currency and booked.foreign_amount == row.tx.amount:
        return True
    if row.conversion is None:
        return booked.amount == row.tx.amount
    return (booked.amount < 0) == (row.amount < 0) and abs(booked.amount - row.amount) <= abs(row.amount) * RATE_TOLERANCE


def save_rules(rows: list[Row], store: Store) -> None:
    for row in rows:
        if not row.include:
            continue
        if row.remember == ALWAYS_ASK:
            store.always_ask(row.counterparty)
        elif row.remember == REMEMBER and row.transfer_with:
            store.remember_transfer(row.counterparty, row.transfer_with.name)
        elif row.remember == REMEMBER and row.category:
            store.remember(row.counterparty, row.category)


def send(rows: list[Row], account_id: str, firefly: FireflyClient) -> list[Outcome]:
    outcomes = []
    for number, row in enumerate(rows):
        if not row.include:
            outcomes.append(Outcome(row, SKIPPED))
            continue
        split = split_for(
            row.as_sent(), account_id, row.category or None, conversion=row.conversion,
            transfer_with=row.transfer_with.id if row.transfer_with else None,
        )
        try:
            firefly_id = firefly.create_transaction([split])
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
