"""The web app end to end, against a fake Firefly."""

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from firefly_uploader.auth import TokenCipher
from firefly_uploader.firefly import FireflyClient
from firefly_uploader.rates import DailyRates, RatesError
from firefly_uploader.store import Rule, Store
from firefly_uploader.web import create_app

FIXTURES = Path(__file__).parent / "fixtures"
UBS = (FIXTURES / "ubs_sample.csv").read_bytes()
REVOLUT_EUR = (FIXTURES / "revolut_eur_sample.csv").read_bytes()
USER = "test@example.com @ https://firefly.example"
APP_USER = 1  # the first user, made by logged_in()
CONNECTION = "/c/1"
TOKEN = "good-token"


def page(items) -> dict:
    return {"data": items, "meta": {"pagination": {"current_page": 1, "total_pages": 1}}}


class FakeFirefly:
    """Just enough of the Firefly API; remembers what was created, refuses exact copies."""

    url = "https://firefly.example"

    def __init__(self):
        self.accounts = {"12": ("UBS", "CHF"), "15": ("Cash wallet", "CHF"), "16": ("Revolut", "CHF"),
                         "17": ("Broker", "CHF")}
        self.categories = ["Groceries", "Investment", "Public Transport"]
        self.groups: dict[str, list[dict]] = {}  # per account
        self.created: list[dict] = []

    def connect(self, url: str, token: str) -> FireflyClient:
        return FireflyClient(url, token, transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Unauthenticated."})
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
        source, destination = split.get("source_id", "90"), split.get("destination_id", "91")
        group = {"id": group_id, "attributes": {"transactions": [{
            "date": split["date"] + "T00:00:00+02:00", "amount": split["amount"],
            "source_id": source, "destination_id": destination,
            "description": split["description"], "external_id": split["external_id"],
            "foreign_amount": split.get("foreign_amount"), "foreign_currency_code": split.get("foreign_currency_code"),
        }]}}
        for account in {source, destination} & self.accounts.keys():  # a transfer shows on both
            self.groups.setdefault(account, []).append(group)
        return httpx.Response(200, json={"data": {"id": group_id}})


@pytest.fixture
def firefly() -> FakeFirefly:
    return FakeFirefly()


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "uploader.db", TokenCipher(TokenCipher.new_key()))


def fake_rates(source, target, start, end) -> DailyRates:
    return DailyRates(source, target, {date(2026, 8, 31): Decimal("0.94")})


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html)[1]


def logged_in(app) -> TestClient:
    """A browser that created the first user and added the fake Firefly as default connection."""
    browser = TestClient(app)
    home = browser.post("/setup", data={"username": "alice", "password": "a long password",
                                        "password_again": "a long password"})
    browser.csrf = csrf_of(home.text)
    browser.post("/connections", data={"csrf": browser.csrf, "url": FakeFirefly.url, "token": TOKEN,
                                       "make_default": "1"})
    return browser


@pytest.fixture
def app(firefly, store):
    return create_app(store, connect=firefly.connect, rates=fake_rates)


@pytest.fixture
def browser(app) -> TestClient:
    return logged_in(app)


def upload(browser, data=UBS, filename="ubs.csv") -> httpx.Response:
    return browser.post(f"{CONNECTION}/upload", data={"csrf": browser.csrf},
                        files={"statement_file": (filename, data, "text/csv")})


def send(browser, review_url: str, form: dict) -> httpx.Response:
    return browser.post(review_url, data={"csrf": browser.csrf, **form})


def rows_of(html: str) -> list[str]:
    """The rows of the page's (one) table of transactions."""
    body = re.search(r"<tbody>(.*?)</tbody>", html, re.DOTALL)[1]
    return re.findall(r"<tr\b.*?</tr>", body, re.DOTALL)


def counts_of(html: str) -> dict[str, int]:
    """The result page's counts: created, duplicate, failed, skipped."""
    return {kind: int(n) for kind, n in re.findall(r'data-count="(\w+)">(\d+)<', html)}


def test_shows_which_firefly_user_is_used(browser):
    html = browser.get(CONNECTION).text

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

    result = send(browser, review_url, form)

    assert result.status_code == 200
    assert counts_of(result.text) == {"created": 2, "duplicate": 0, "failed": 0, "skipped": 6}
    assert [(s["source_id"], s["destination_name"], s.get("category_name")) for s in firefly.created] == [
        ("12", "SBB EASYRIDE", "Public Transport"), ("12", "MUSTER BAECKEREI AG", "Groceries"),
    ]
    assert store.rules(APP_USER) == {"sbb easyride": Rule("Public Transport")}
    assert store.linked_account(USER, "CH1234567890123456789") == "12"


def test_second_review_marks_rows_already_uploaded_and_suggests_category(browser, firefly):
    review_url = str(upload(browser).url)
    send(browser, review_url, {"account": "12", "include": ["0"],
                               "category_0": "Public Transport", "remember_0": "remember"})

    rows = rows_of(browser.get(review_url).text)

    assert "Already uploaded" in rows[0] and 'value="0" checked' not in rows[0]
    assert 'remembered"><i' in rows[4]  # another SBB ride
    assert "<option selected>Public Transport</option>" in rows[4]


def test_sending_twice_is_refused_by_firefly(browser, firefly):
    review_url = str(upload(browser).url)
    form = {"account": "12", "include": ["0"], "remember_0": "once"}
    send(browser, review_url, form)

    result = send(browser, review_url, form)

    assert counts_of(result.text) | {"skipped": 0} == {"created": 0, "duplicate": 1, "failed": 0, "skipped": 0}
    assert len(firefly.created) == 1


def test_foreign_currency_is_converted(browser, firefly):
    response = upload(browser, REVOLUT_EUR, "revolut.csv")
    review_url = str(response.url)

    assert '<option value="16" selected>Revolut (CHF)</option>' in response.text
    assert "converted from EUR to CHF" in response.text
    assert "-37.60&nbsp;CHF" in rows_of(response.text)[0]  # -40.00 EUR at 0.94
    assert "-40.00&nbsp;EUR at 0.94 (2026-08-31)" in rows_of(response.text)[0]

    send(browser, review_url, {"account": "16", "include": ["0"], "remember_0": "once"})

    [split] = firefly.created
    assert (split["amount"], split["currency_code"]) == ("37.60", "CHF")
    assert (split["foreign_amount"], split["foreign_currency_code"]) == ("40.00", "EUR")
    assert "Already uploaded" in rows_of(browser.get(review_url).text)[0]


def test_no_rates_no_sending(firefly, store):
    def no_rates(*args):
        raise RatesError("Couldn't get EUR to CHF exchange rates from frankfurter.dev: offline")

    browser = logged_in(create_app(store, connect=firefly.connect, rates=no_rates))
    response = upload(browser, REVOLUT_EUR, "revolut.csv")

    assert "Couldn&#39;t get EUR to CHF exchange rates" in response.text
    assert 'id="send" disabled data-blocked>' in response.text
    assert send(browser, str(response.url), {"account": "16", "include": ["0"]}).status_code == 502
    assert firefly.created == []


def test_transfer_to_own_account_is_offered_and_remembered(browser, firefly, store):
    response = upload(browser)
    review_url = str(response.url)
    broker_row = 6  # Example Broker Ltd.

    assert '<option value="transfer:17">Broker</option>' in rows_of(response.text)[broker_row]
    assert '<option value="transfer:12">' not in response.text  # not the statement's own account

    send(browser, review_url, {"account": "12", "include": [str(broker_row)],
                               f"category_{broker_row}": "transfer:17", f"remember_{broker_row}": "remember"})

    [split] = firefly.created
    assert (split["type"], split["source_id"], split["destination_id"], split["amount"]) == ("transfer", "12", "17", "500.00")
    assert store.rules(APP_USER) == {"example broker ltd.": Rule(transfer_account="Broker")}
    row = rows_of(browser.get(review_url).text)[broker_row]
    assert '<option value="transfer:17" selected>Broker</option>' in row
    assert 'remembered"><i' in row


def test_switching_account(browser):
    review_url = str(upload(browser).url)

    html = browser.get(review_url, params={"account": "15"}).text

    assert '<option value="15" selected>Cash wallet (CHF)</option>' in html


def test_forgotten_upload(browser):
    response = browser.get(f"{CONNECTION}/review/" + "0" * 32)

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

    send(browser, review_url, form)

    [split] = firefly.created
    assert (split["destination_name"], split["description"]) == ("Muster Bakery", "Muster Bakery")
    assert split["notes"].startswith("Name in the statement: MUSTER BAECKEREI AG")
    assert store.rules(APP_USER) == {"muster bakery": Rule("Groceries")}


# Logging in and Firefly connections


def test_first_start_asks_for_a_user(app):
    browser = TestClient(app)

    response = browser.get("/c/1")

    assert response.url.path == "/setup"
    assert "Create the user you'll log in with" in response.text


def test_setup_checks_the_password(app, store):
    response = TestClient(app).post("/setup", data={"username": "alice", "password": "short", "password_again": "short"})

    assert response.status_code == 400
    assert "at least 8 characters" in response.text
    assert not store.has_users()


def test_setup_then_add_the_first_connection(app, store):
    browser = TestClient(app)

    home = browser.post("/setup", data={"username": "alice", "password": "a long password",
                                        "password_again": "a long password"})

    assert home.url.path == "/connections"
    assert "Add the Firefly user you want to send statements to" in home.text
    assert 'name="make_default" value="1" checked>' in home.text  # the first one becomes the default
    assert TestClient(app).get("/setup").url.path == "/login"  # only once


def test_adding_a_connection_checks_the_token(app, store):
    browser = logged_in(app)

    response = browser.post("/connections", data={"csrf": browser.csrf, "url": FakeFirefly.url, "token": "wrong"})

    assert response.status_code == 400
    assert "Firefly rejected the access token" in response.text
    assert len(store.connections(APP_USER)) == 1  # only the one logged_in() added


def test_adding_a_connection_cleans_up_the_address(app, store):
    browser = TestClient(app)
    home = browser.post("/setup", data={"username": "alice", "password": "a long password",
                                        "password_again": "a long password"})

    response = browser.post("/connections", data={"csrf": csrf_of(home.text), "url": " firefly.example/api/v1/ ",
                                                  "token": f" {TOKEN}\n"})

    assert response.url.path == "/c/1"
    [connection] = store.connections(APP_USER)
    assert (connection.url, connection.firefly_user, connection.is_default) == (
        "https://firefly.example", "test@example.com", False,
    )
    assert store.connection(APP_USER, 1).token == TOKEN


def test_login_opens_the_default_connection(app):
    logged_in(app)
    browser = TestClient(app)

    wrong = browser.post("/login", data={"username": "alice", "password": "not it"})
    right = browser.post("/login", data={"username": "alice", "password": "a long password"})

    assert wrong.status_code == 401 and "Wrong username or password" in wrong.text
    assert right.url.path == CONNECTION


def test_login_without_default_shows_the_connections(app, store):
    logged_in(app)
    store.set_default(APP_USER, 1, is_default=False)

    response = TestClient(app).post("/login", data={"username": "alice", "password": "a long password"})

    assert response.url.path == "/connections"
    assert '<a href="/c/1" class="fw-semibold">test@example.com</a>' in response.text


def test_login_returns_to_the_page_asked_for(app):
    logged_in(app)
    browser = TestClient(app)

    login_page = browser.get("/c/1/review/abc")
    response = browser.post("/login", data={"username": "alice", "password": "a long password",
                                            "next": "/c/1/review/abc"})

    assert login_page.url.path == "/login" and 'value="/c/1/review/abc"' in login_page.text
    assert response.url.path == "/c/1/review/abc"


def test_login_never_sends_elsewhere(app):
    logged_in(app)

    response = TestClient(app).post("/login", data={"username": "alice", "password": "a long password",
                                                    "next": "//evil.example"})

    assert response.url.path == CONNECTION


def test_opening_the_app_while_logged_in_goes_to_the_default(browser, store):
    assert browser.get("/").url.path == CONNECTION
    store.set_default(APP_USER, 1, is_default=False)
    assert browser.get("/").url.path == "/connections"


def test_default_can_be_changed(browser, store):
    browser.post("/connections/1/default", data={"csrf": browser.csrf, "is_default": "0"})

    assert store.default_connection(APP_USER) is None
    assert "Make default" in browser.get("/connections").text


def test_forms_without_the_session_token_are_refused(browser, firefly):
    response = browser.post("/connections/1/remove", data={"csrf": "forged"})

    assert response.status_code == 403
    assert browser.get(CONNECTION).status_code == 200  # still there


def test_remove_connection(browser, store):
    browser.post("/connections/1/remove", data={"csrf": browser.csrf})

    assert store.connections(APP_USER) == []
    assert browser.get(CONNECTION).status_code == 404


def test_logout(browser):
    browser.post("/logout", data={"csrf": browser.csrf})

    assert browser.get(CONNECTION).url.path == "/login"


def test_connections_of_other_users_are_hidden(app, store):
    logged_in(app)
    store.create_user("bob", "another password")
    bob = TestClient(app)
    bob.post("/login", data={"username": "bob", "password": "another password"})

    assert bob.get(CONNECTION).status_code == 404


def logged_in_as_alice(app) -> TestClient:
    browser = TestClient(app)
    browser.post("/login", data={"username": "alice", "password": "a long password"})
    return browser


def test_unreadable_token_says_what_to_do(app, store, firefly):
    logged_in(app)
    other_key = Store(store.path, TokenCipher(TokenCipher.new_key()))
    browser = logged_in_as_alice(create_app(other_key, connect=firefly.connect, rates=fake_rates))

    response = browser.get(CONNECTION)

    assert response.status_code == 409
    assert "the app&#39;s secret key changed" in response.text


def test_health_check_needs_no_login(app):
    assert TestClient(app).get("/healthz").text == "ok"
