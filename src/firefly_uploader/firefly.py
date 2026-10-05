"""Talk to the Firefly III API (v1): read accounts and categories, create transactions."""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

import httpx
from dotenv import load_dotenv

from .models import Transaction


class FireflyError(Exception):
    """Firefly refused a request. The message is meant to be shown to the user."""


class FireflyUnreachableError(FireflyError):
    pass


class DuplicateTransactionError(FireflyError):
    """Firefly already has an identical transaction."""


@dataclass
class Account:
    id: str
    name: str
    currency: str
    iban: str | None = None
    active: bool = True


class FireflyClient:
    def __init__(self, url: str, token: str, *, transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.url + "/api/v1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.api+json"},
            timeout=30,
            transport=transport,
        )

    @classmethod
    def from_env(cls, prefix: str = "FIREFLY") -> "FireflyClient":
        """Connect with <prefix>_URL and <prefix>_TOKEN, from the environment or `.env`."""
        load_dotenv()
        url, token = os.environ.get(f"{prefix}_URL"), os.environ.get(f"{prefix}_TOKEN")
        if not url or not token:
            raise FireflyError(f"{prefix}_URL and {prefix}_TOKEN aren't set (put them in .env)")
        return cls(url, token)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "FireflyClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def version(self) -> str:
        return self._request("GET", "/about")["data"]["version"]

    def user_email(self) -> str:
        return self._request("GET", "/about/user")["data"]["attributes"]["email"]

    def asset_accounts(self) -> list[Account]:
        return [_account(item) for item in self._pages("/accounts", {"type": "asset"})]

    def categories(self) -> list[str]:
        return sorted(item["attributes"]["name"] for item in self._pages("/categories"))

    def external_ids(self, account_id: str, start: date, end: date) -> set[str]:
        """External IDs of the transactions booked on an account between two dates (inclusive)."""
        # Firefly refuses start == end; a day too many is harmless for spotting duplicates.
        end = max(end, start + timedelta(days=1))
        params = {"start": start.isoformat(), "end": end.isoformat()}
        return {
            split["external_id"]
            for group in self._pages(f"/accounts/{account_id}/transactions", params)
            for split in group["attributes"]["transactions"]
            if split.get("external_id")
        }

    def create_transaction(self, splits: list[dict]) -> str:
        """Create a transaction from one or more splits and return its ID."""
        body = {"error_if_duplicate_hash": True, "transactions": splits}
        return self._request("POST", "/transactions", json=body)["data"]["id"]

    def _pages(self, path: str, params: dict | None = None) -> Iterator[dict]:
        page = 1
        while True:
            body = self._request("GET", path, params={**(params or {}), "limit": 100, "page": page})
            yield from body["data"]
            if page >= body["meta"]["pagination"]["total_pages"]:
                return
            page += 1

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise FireflyUnreachableError(f"Can't reach Firefly at {self.url}: {error}") from error
        if response.status_code == 401:
            raise FireflyError("Firefly rejected the access token")
        try:
            body = response.json()
        except ValueError:
            raise FireflyError(
                f"{method} {path}: unexpected answer from Firefly (HTTP {response.status_code})"
            ) from None
        if response.is_success:
            return body
        message = body.get("message") or f"HTTP {response.status_code}"
        if message.startswith("Duplicate of transaction"):
            raise DuplicateTransactionError(message)
        details = "; ".join(f"{field}: {' '.join(texts)}" for field, texts in body.get("errors", {}).items())
        raise FireflyError(f"{method} {path}: {details or message}")


def _account(item: dict) -> Account:
    attributes = item["attributes"]
    return Account(
        id=item["id"],
        name=attributes["name"],
        currency=attributes["currency_code"],
        iban=attributes.get("iban") or None,
        active=attributes["active"],
    )


def split_for(tx: Transaction, account_id: str, category: str | None = None) -> dict:
    """Firefly split for money leaving (withdrawal) or arriving on (deposit) an asset account."""
    if tx.amount < 0:
        split = {"type": "withdrawal", "source_id": account_id, "destination_name": tx.counterparty}
    else:
        split = {"type": "deposit", "source_name": tx.counterparty, "destination_id": account_id}
    split |= {
        "date": tx.date.isoformat(),
        "amount": str(abs(tx.amount)),
        "currency_code": tx.currency,
        "description": tx.description or tx.counterparty,
        "external_id": tx.external_id,
    }
    if tx.book_date:
        split["book_date"] = tx.book_date.isoformat()
    if tx.notes:
        split["notes"] = tx.notes
    if category:
        split["category_name"] = category
    return split
