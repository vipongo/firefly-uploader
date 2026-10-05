from datetime import date
from decimal import Decimal

import httpx
import pytest

from firefly_uploader.rates import DailyRates, RatesError, fetch_rates

ECB = {"2026-09-03": {"CHF": 0.939, "HUF": 362.5}, "2026-09-04": {"CHF": 0.9405, "HUF": 363.28}}


def frankfurter(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/v1/2026-08-28..2026-09-06"  # a week early, for weekends and holidays
    assert request.url.params["base"] == "EUR"
    symbols = request.url.params["symbols"].split(",")
    rates = {day: {c: rate for c, rate in by_currency.items() if c in symbols} for day, by_currency in ECB.items()}
    return httpx.Response(200, json={"amount": 1.0, "base": "EUR", "rates": rates})


def test_euro_rates_are_used_as_published():
    rates = fetch_rates("EUR", "CHF", date(2026, 9, 4), date(2026, 9, 6), transport=httpx.MockTransport(frankfurter))

    assert rates.by_day[date(2026, 9, 4)] == Decimal("0.9405")


def test_other_currencies_go_through_the_euro():
    rates = fetch_rates("HUF", "CHF", date(2026, 9, 4), date(2026, 9, 6), transport=httpx.MockTransport(frankfurter))

    converted = rates.convert(Decimal("-10000"), date(2026, 9, 4))

    assert converted.amount == Decimal("-25.89")  # 10000 / 363.28 * 0.9405 = 25.889...
    assert converted.currency == "CHF"


def test_weekend_uses_the_last_business_day():
    rates = DailyRates("EUR", "CHF", {date(2026, 9, 3): Decimal("0.939"), date(2026, 9, 4): Decimal("0.9405")})

    converted = rates.convert(Decimal("10.00"), date(2026, 9, 6))

    assert (converted.amount, converted.rate_date) == (Decimal("9.41"), date(2026, 9, 4))


def test_no_rate_before_the_first_day():
    rates = DailyRates("EUR", "CHF", {date(2026, 9, 4): Decimal("0.9405")})

    with pytest.raises(RatesError, match="No EUR to CHF rate for 2026-09-01"):
        rates.convert(Decimal("10.00"), date(2026, 9, 1))


def test_service_down():
    def handler(request):
        return httpx.Response(503, text="maintenance")

    with pytest.raises(RatesError, match="Couldn't get EUR to CHF exchange rates from frankfurter.dev"):
        fetch_rates("EUR", "CHF", date(2026, 9, 4), date(2026, 9, 6), transport=httpx.MockTransport(handler))
