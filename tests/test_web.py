"""The web app end to end, against a fake Firefly."""

import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from firefly_uploader.firefly import FireflyClient
from firefly_uploader.store import Rule, Store
from firefly_uploader.web import create_app

FIXTURES = Path(__file__).parent / "fixtures"
UBS = (FIXTURES / "ubs_sample.csv").read_bytes()
REVOLUT_EUR = (FIXTURES / "revolut_eur_sample.csv").read_bytes()
USER = "test@example.com @ https://firefly.example"


def page(items) -> dict:
    return {"data": items, "meta": {"pagination": {"current_page": 1, "total_pages": 1}}}


class FakeFirefly:
    """Just enough of the Firefly API; remembers what was created, refuses exact copies."""

    url = "https://firefly.example"

    def __init__(self):
        self.accounts = {"12": ("UBS", "CHF"), "15": ("Cash wallet", "CHF"), "16": ("Revolut", "CHF")}
        self.categories = ["Groceries", "Investment", "Public Transport"]
        self.groups: dict[str, list[dict]] = {}  # per account
        self.created: list[dict] = []

    def connect(self) -> FireflyClient:
        return FireflyClient(self.url, "token", transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/about":
            return httpx.Response(200, json={"data": {"version": "6.7.7"}})
        if path == "/about/user":
            return httpx.Response(200, json={"data": {"attributes": {"email": "test@example.com"}}})
        if path == "/accounts" and request.url.params["type"] == "asset":
            return httpx.Response(200, json=page([
                {"id": id, "attributes": {"name": name, "currency_code": currency, "iban": None, "active": True}}
                for id, (name, currency) in self.accounts.items()
            ]))
        if path == "/accounts":  # expense or revenue: the shops and people paid before
            names = {"expense": ["Jane Doe", "SBB EASYRIDE"], "revenue": ["Employer AG"]}
            return httpx.Response(200, json=page([
                {"id": name, "attributes": {"name": name}} for name in names[request.url.params["type"]]
            ]))
        if path == "/categories":
            return httpx.Response(200, json=page([{"id": n, "attributes": {"name": n}} for n in self.categories]))
        if match := re.fullmatch(r"/accounts/(\d+)/transactions", path):
            return httpx.Response(200, json=page(self.groups.get(match[1], [])))
        if path == "/transactions" and request.method == "POST":
            return self.create(json.loads(request.content)["transactions"][0])
        return httpx.Response(404, json={"message": f"no fake for {path}"})

    def create(self, split: dict) -> httpx.Response:
        if split in self.created:
            return httpx.Response(422, json={"message": "Duplicate of transaction #1."})
        self.created.append(split)
        group_id = str(100 + len(self.created))
        account = split.get("source_id") or split["destination_id"]
        self.groups.setdefault(account, []).append({"id": group_id, "attributes": {"transactions": [{
            "date": split["date"] + "T00:00:00+02:00", "amount": split["amount"],
            "source_id": split.get("source_id", "90"), "destination_id": split.get("destination_id", "91"),
            "description": split["description"], "external_id": split["external_id"],
        }]}})
        return httpx.Response(200, json={"data": {"id": group_id}})


@pytest.fixture
def firefly() -> FakeFirefly:
    return FakeFirefly()


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "uploader.db")


@pytest.fixture
def browser(firefly, store) -> TestClient:
    return TestClient(create_app(firefly.connect, store))


def upload(browser, data=UBS, filename="ubs.csv") -> httpx.Response:
    return browser.post("/upload", files={"statement_file": (filename, data, "text/csv")})


def rows_of(html: str) -> list[str]:
    return re.findall(r"<tr(?: class=\"[^\"]*\")?>\s*<td>.*?</tr>", html, re.DOTALL)


def test_shows_which_firefly_user_is_used(browser):
    html = browser.get("/").text

    assert "Firefly 6.7.7 at firefly.example as <strong>test@example.com</strong>" in html


def test_rejects_unknown_files(browser):
    response = upload(browser, b"Date,Amount\n2026-10-01,12.00\n", "other.csv")

    assert response.status_code == 400
    assert "Couldn&#39;t read other.csv" in response.text


def test_review_picks_account_by_name_and_lists_rows(browser):
    response = upload(browser)

    assert re.search(r"/review/[0-9a-f]{32}$", str(response.url))
    assert '<option value="12" selected>UBS (CHF)</option>' in response.text
    assert len(rows_of(response.text)) == 8
    assert "<option>Public Transport</option>" in response.text


def test_send_creates_transactions_and_remembers(browser, firefly, store):
    review_url = str(upload(browser).url)
    form = {"account": "12", "include": ["0", "2"],
            "category_0": "Public Transport", "remember_0": "remember",
            "category_2": "Groceries", "remember_2": "once"}

    result = browser.post(review_url, data=form)

    assert result.status_code == 200
    assert "2 created" in result.text and "6 left out" in result.text
    assert [(s["source_id"], s["destination_name"], s.get("category_name")) for s in firefly.created] == [
        ("12", "SBB EASYRIDE", "Public Transport"), ("12", "MUSTER BAECKEREI AG", "Groceries"),
    ]
    assert store.rules() == {"sbb easyride": Rule("Public Transport")}
    assert store.linked_account(USER, "CH1234567890123456789") == "12"


def test_second_review_marks_rows_already_uploaded_and_suggests_category(browser, firefly):
    review_url = str(upload(browser).url)
    browser.post(review_url, data={"account": "12", "include": ["0"],
                                   "category_0": "Public Transport", "remember_0": "remember"})

    rows = rows_of(browser.get(review_url).text)

    assert "Already uploaded" in rows[0] and 'value="0" checked' not in rows[0]
    assert 'class="small remembered">' in rows[4]  # another SBB ride
    assert "<option selected>Public Transport</option>" in rows[4]


def test_sending_twice_is_refused_by_firefly(browser, firefly):
    review_url = str(upload(browser).url)
    form = {"account": "12", "include": ["0"], "remember_0": "once"}
    browser.post(review_url, data=form)

    result = browser.post(review_url, data=form)

    assert "0 created · 1 already in Firefly" in result.text
    assert len(firefly.created) == 1


def test_foreign_currency_is_blocked_for_now(browser, firefly):
    response = upload(browser, REVOLUT_EUR, "revolut.csv")

    assert '<option value="16" selected>Revolut (CHF)</option>' in response.text
    assert "This statement is in EUR but Revolut is in CHF" in response.text
    assert '<button id="send" disabled data-blocked>' in response.text
    assert browser.post(str(response.url), data={"account": "16", "include": ["0"]}).status_code == 400
    assert firefly.created == []


def test_switching_account(browser):
    review_url = str(upload(browser).url)

    html = browser.get(review_url, params={"account": "15"}).text

    assert '<option value="15" selected>Cash wallet (CHF)</option>' in html


def test_forgotten_upload(browser):
    response = browser.get("/review/" + "0" * 32)

    assert response.status_code == 404
    assert "upload the file again" in response.text


def test_review_suggests_known_names(browser):
    html = upload(browser, REVOLUT_EUR, "revolut.csv").text

    assert '<datalist id="names"><option value="Employer AG"><option value="Jane Doe">' in html
    assert 'name="name_1" value="Revolut Bank UAB"' in html


def test_name_can_be_changed(browser, firefly, store):
    review_url = str(upload(browser).url)
    form = {"account": "12", "include": ["2"], "name_2": "  Muster   Bakery ",
            "category_2": "Groceries", "remember_2": "remember"}

    browser.post(review_url, data=form)

    [split] = firefly.created
    assert (split["destination_name"], split["description"]) == ("Muster Bakery", "Muster Bakery")
    assert split["notes"].startswith("Name in the statement: MUSTER BAECKEREI AG")
    assert store.rules() == {"muster bakery": Rule("Groceries")}
