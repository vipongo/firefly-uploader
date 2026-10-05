"""Small helpers shared by the bank parsers."""

import hashlib
from datetime import date, datetime
from decimal import Decimal


def to_decimal(text: str | None) -> Decimal | None:
    """'-2000.00' or "2'000.00" -> Decimal; empty -> None."""
    if text is None:
        return None
    cleaned = text.strip().replace("'", "").replace("’", "").replace(" ", "")
    return Decimal(cleaned) if cleaned else None


def parse_date(text: str) -> date:
    """ISO dates, or the Swiss 05.10.2026 style some exports use."""
    text = text.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return datetime.strptime(text, "%d.%m.%Y").date()


def normalize_iban(text: str) -> str:
    return text.replace(" ", "").upper()


def fingerprint(*parts: object) -> str:
    """Stable ID for banks that don't give each transaction a number."""
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]
