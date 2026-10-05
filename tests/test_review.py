from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from firefly_uploader import review
from firefly_uploader.firefly import (
    Account,
    Booked,
    DuplicateTransactionError,
    FireflyError,
    FireflyUnreachableError,
)
from firefly_uploader.models import Statement, Transaction
from firefly_uploader.store import Rule, Store


def tx(counterparty="SBB EASYRIDE", amount="-6.50", day=5, external_id=None) -> Transaction:
    return Transaction(
        date=date(2026, 10, day), amount=Decimal(amount), currency="CHF", counterparty=counterparty,
        description=counterparty, external_id=external_id or f"{counterparty}-{day}-{amount}",
    )


def statement(*transactions) -> Statement:
    return Statement(bank="ubs", account="CH1234567890123456789", currency="CHF",
                     transactions=list(transactions))


def booked(amount="-6.50", day=5, external_id=None, id="1") -> Booked:
    return Booked(id=id, date=date(2026, 10, day), amount=Decimal(amount), description="typed in",
                  external_id=external_id)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "uploader.db")


def test_remembered_category_is_filled_in():
    [known, asked, new] = review.prepare(
        statement(tx("SBB EasyRide"), tx("Amazon"), tx("Bakery")), [],
        {"sbb easyride": Rule("Public Transport"), "amazon": Rule(always_ask=True)},
    )

    assert (known.category, known.from_rule, known.remember) == ("Public Transport", True, review.REMEMBER)
    assert (asked.category, asked.remember) == ("", review.ALWAYS_ASK)
    assert (new.category, new.from_rule, new.remember) == ("", False, review.REMEMBER)


def test_uploaded_before_is_found_by_external_id():
    [row] = review.prepare(statement(tx(external_id="X1")), [booked(amount="-99", external_id="X1")], {})

    assert row.in_firefly and row.same_id
    assert not row.include


def test_typed_in_by_hand_is_found_by_amount_and_close_date():
    [near, far] = review.prepare(
        statement(tx(amount="-12.00", day=5), tx(amount="-30.00", day=5)),
        [booked(amount="-12.00", day=3), booked(amount="-30.00", day=1)],
        {},
    )

    assert near.in_firefly and not near.same_id and not near.include
    assert far.in_firefly is None and far.include


def test_each_firefly_transaction_matches_one_row():
    rows = review.prepare(statement(tx(day=4), tx(day=5)), [booked(day=5)], {})

    assert [row.in_firefly is not None for row in rows] == [False, True]


def test_money_in_does_not_match_money_out():
    [row] = review.prepare(statement(tx(amount="6.50")), [booked(amount="-6.50")], {})

    assert row.in_firefly is None


def test_search_range_leaves_room_for_shifted_dates():
    assert review.search_range(statement(tx(day=2), tx(day=5))) == (date(2026, 9, 29), date(2026, 10, 8))


@pytest.mark.parametrize(("linked", "expected"), [("16", "16"), (None, "12"), ("404", "12")])
def test_pick_account(linked, expected):
    accounts = [Account("16", "Revolut", "CHF"), Account("12", "UBS Privatkonto", "CHF")]

    assert review.pick_account(statement(tx()), accounts, linked).id == expected


def test_pick_account_by_iban_before_name():
    accounts = [Account("12", "UBS", "CHF"), Account("13", "Savings", "CHF", iban="CH12 3456 7890 1234 5678 9")]

    assert review.pick_account(statement(tx()), accounts, None).id == "13"


def test_pick_account_finds_nothing():
    assert review.pick_account(statement(tx()), [Account("15", "Cash wallet", "CHF")], None) is None


def test_save_rules(store):
    rows = review.prepare(statement(tx("SBB"), tx("Amazon"), tx("Bakery"), tx("Kiosk")), [], {})
    choices = [("Public Transport", review.REMEMBER, True), ("Groceries", review.ALWAYS_ASK, True),
               ("Groceries", review.ONCE, True), ("Groceries", review.REMEMBER, False)]
    for row, (category, remember, include) in zip(rows, choices):
        row.category, row.remember, row.include = category, remember, include

    review.save_rules(rows, store)

    assert store.rules() == {"sbb": Rule("Public Transport"), "amazon": Rule(always_ask=True)}


class FakeFirefly:
    def __init__(self, *results):
        self.results = list(results)
        self.sent = []

    def create_transaction(self, splits):
        self.sent.append(splits)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_send_reports_each_row():
    rows = review.prepare(statement(tx("A"), tx("B"), tx("C"), tx("D")), [], {})
    rows[0].category = "Groceries"
    rows[3].include = False
    firefly = FakeFirefly("101", DuplicateTransactionError("Duplicate of transaction #7."), FireflyError("bad"))

    outcomes = review.send(rows, "12", firefly)

    assert [(o.status, o.firefly_id) for o in outcomes] == [
        (review.CREATED, "101"), (review.DUPLICATE, None), (review.FAILED, None), (review.SKIPPED, None),
    ]
    assert firefly.sent[0][0]["category_name"] == "Groceries"
    assert "category_name" not in firefly.sent[1][0]


def test_send_stops_when_firefly_goes_away():
    rows = review.prepare(statement(tx("A"), tx("B"), tx("C")), [], {})
    rows[2].include = False
    firefly = FakeFirefly(FireflyUnreachableError("Can't reach Firefly"))

    outcomes = review.send(rows, "12", firefly)

    assert [o.status for o in outcomes] == [review.FAILED, review.FAILED, review.SKIPPED]
    assert len(firefly.sent) == 1


def test_rules_ignore_case_and_spacing(store):
    store.remember("SBB  EasyRide", "Public Transport")
    store.remember("sbb easyride", "Travel Home")

    assert store.rules() == {"sbb easyride": Rule("Travel Home")}


def test_account_links_belong_to_one_firefly_user(store):
    store.link_account("test@example.com @ https://firefly.example", "CH12", "12")

    assert store.linked_account("test@example.com @ https://firefly.example", "CH12") == "12"
    assert store.linked_account("real@example.com @ https://firefly.example", "CH12") is None


def test_renamed_row_is_sent_under_the_new_name():
    revolut = tx("Revolut Bank UAB", amount="25.00")
    [row] = review.prepare(statement(revolut), [], {})
    row.counterparty, row.category = "Jane Doe", "Gifts"
    firefly = FakeFirefly("101")

    review.send([row], "16", firefly)

    [[split]] = firefly.sent
    assert (split["source_name"], split["description"]) == ("Jane Doe", "Jane Doe")
    assert split["notes"] == "Name in the statement: Revolut Bank UAB"
    assert row.tx.counterparty == "Revolut Bank UAB"  # the statement itself is unchanged


def test_renaming_keeps_a_description_that_says_more():
    transfer = Transaction(date=date(2026, 9, 3), amount=Decimal("-40.00"), currency="CHF",
                           counterparty="Jane Doe", description="To Jane Doe", external_id="r1")
    row = review.Row(transfer, counterparty="Jane Smith")

    assert row.as_sent().description == "To Jane Doe"


def test_rules_are_saved_under_the_chosen_name(store):
    [row] = review.prepare(statement(tx("Revolut Bank UAB")), [], {})
    row.counterparty, row.category = "Jane Doe", "Gifts"

    review.save_rules([row], store)

    assert store.rules() == {"jane doe": Rule("Gifts")}
