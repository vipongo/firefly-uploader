"""Bank-independent representation of a statement and its transactions."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class Transaction:
    date: date  # when the purchase or payment happened
    amount: Decimal  # signed, in `currency`: negative = money out, fees included
    currency: str
    counterparty: str  # cleaned name used for matching, e.g. "SBB EASYRIDE"
    description: str  # short human-readable text from the bank
    external_id: str  # unique per bank transaction, used to avoid double imports
    book_date: date | None = None
    method: str | None = None  # as the bank calls it: "Payment UBS TWINT", "Card Payment"
    counterparty_iban: str | None = None
    fee: Decimal = Decimal("0")
    is_reversal: bool = False
    notes: str = ""  # full original text, kept for reference


@dataclass
class Conversion:
    """A bank amount in the currency of the Firefly account it is booked on."""

    amount: Decimal  # signed, in `currency`, rounded to cents
    currency: str
    rate: Decimal  # units of `currency` per unit of the bank's currency
    rate_date: date  # the business day the rate is from


@dataclass
class Statement:
    bank: str  # "ubs" or "revolut"
    account: str  # IBAN for UBS, "revolut-EUR" style for Revolut
    currency: str
    transactions: list[Transaction] = field(default_factory=list)
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    warnings: list[str] = field(default_factory=list)
