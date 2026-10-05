"""Daily exchange rates of the European Central Bank, from frankfurter.dev (free, no key)."""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import httpx

from .models import Conversion

API = "https://api.frankfurter.dev/v1"
CENT = Decimal("0.01")


class RatesError(Exception):
    """No rate to be had. The message is meant to be shown to the user."""


@dataclass
class DailyRates:
    source: str
    target: str
    by_day: dict[date, Decimal]  # units of `target` per unit of `source`, business days only

    def convert(self, amount: Decimal, day: date) -> Conversion:
        """Convert with the rate of `day`, or of the last business day before it (weekends, holidays)."""
        days = sorted(self.by_day)
        index = bisect_right(days, day)
        if index == 0:
            raise RatesError(f"No {self.source} to {self.target} rate for {day}")
        rate_date = days[index - 1]
        rate = self.by_day[rate_date]
        return Conversion((amount * rate).quantize(CENT, ROUND_HALF_UP), self.target, rate, rate_date)


def fetch_rates(
    source: str, target: str, start: date, end: date, *, transport: httpx.BaseTransport | None = None
) -> DailyRates:
    """`source` to `target` rates for the business days from a week before `start` until `end`."""
    # The ECB publishes rates against the euro. Asking for those and dividing keeps every digit:
    # frankfurter's own HUF to CHF rate is rounded to 0.00259.
    currencies = sorted({source, target} - {"EUR"})
    try:
        with httpx.Client(timeout=20, transport=transport) as http:
            response = http.get(
                f"{API}/{start - timedelta(days=7)}..{end}",
                params={"base": "EUR", "symbols": ",".join(currencies)},
            )
            response.raise_for_status()
            per_euro = response.json(parse_float=Decimal)["rates"]
            by_day = {
                date.fromisoformat(day): _per_euro(rates, target) / _per_euro(rates, source)
                for day, rates in per_euro.items()
            }
    except (httpx.HTTPError, ValueError, KeyError) as error:
        raise RatesError(f"Couldn't get {source} to {target} exchange rates from frankfurter.dev: {error}") from error
    return DailyRates(source, target, by_day)


def _per_euro(rates: dict[str, Decimal], currency: str) -> Decimal:
    return Decimal(1) if currency == "EUR" else rates[currency]
