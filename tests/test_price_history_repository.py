"""Price-history recording and window-context queries."""

import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from rss2discord.delivery_history import PriceHistoryRepository
from rss2discord.delivery_schema import initialize_schema
from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import EntryData, PriceSnapshot
from rss2discord.price_notifications import PriceNotification

NOW = 2_000_000_000
DAY = 86_400

INSERT_HISTORY = (
    "INSERT INTO price_history "
    "(feed_id, product_id, amount, formatted, currency, observed_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def snapshot(
    product_id: str,
    amount: str,
    *,
    feed_id: str = "feed",
    formatted: str | None = None,
    currency: str = "MKD",
) -> PriceSnapshot:
    return PriceSnapshot(
        feed_id=feed_id,
        product_id=product_id,
        amount=Decimal(amount),
        formatted=f"{amount} {currency}" if formatted is None else formatted,
        currency=currency,
    )


def insert_history(
    connection: sqlite3.Connection,
    product_id: str,
    amount: str,
    observed_at: int,
    *,
    feed_id: str = "feed",
    formatted: str | None = None,
    currency: str = "MKD",
) -> None:
    connection.execute(
        INSERT_HISTORY,
        (
            feed_id,
            product_id,
            amount,
            f"{amount} {currency}" if formatted is None else formatted,
            currency,
            observed_at,
        ),
    )


def history_rows(
    store: DeliveryStore,
) -> list[tuple[str, str, str, int]]:
    return [
        (str(product_id), str(amount), str(formatted), int(observed_at))
        for product_id, amount, formatted, observed_at in store._connection.execute(
            "SELECT product_id, amount, formatted, observed_at FROM price_history "
            "ORDER BY product_id, observed_at",
        )
    ]


def backdate_history(store: DeliveryStore, seconds: int) -> None:
    with store._connection:
        store._connection.execute(
            "UPDATE price_history SET observed_at = observed_at - ?",
            (seconds,),
        )


def test_a_new_snapshot_appends_one_history_row(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot("1", "100"))

        rows = history_rows(store)
        assert len(rows) == 1
        product_id, amount, formatted, observed_at = rows[0]
        assert (product_id, amount, formatted) == ("1", "100", "100 MKD")
        assert abs(observed_at - int(time.time())) <= 5


def test_an_amount_change_appends_one_history_row(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot("1", "100"))
        backdate_history(store, 60)
        store.upsert_price_snapshot(snapshot("1", "90"))

        assert [
            (amount, formatted) for _, amount, formatted, _ in history_rows(store)
        ] == [
            ("100", "100 MKD"),
            ("90", "90 MKD"),
        ]


def test_a_formatting_only_change_does_not_append_history(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot("1", "100"))
        store.upsert_price_snapshot(snapshot("1", "100", formatted="100,00 MKD"))

        rows = history_rows(store)
        assert len(rows) == 1
        assert rows[0][:3] == ("1", "100", "100 MKD")
        snapshot_row = store._connection.execute(
            "SELECT amount, formatted FROM price_snapshots WHERE product_id = '1'",
        ).fetchone()
        assert snapshot_row == ("100", "100,00 MKD")


def test_a_currency_change_appends_history(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot("1", "100"))
        backdate_history(store, 60)
        store.upsert_price_snapshot(snapshot("1", "90", currency="EUR"))

        assert [amount for _, amount, _, _ in history_rows(store)] == ["100", "90"]


def test_repeating_the_same_observation_does_not_duplicate_history(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot("1", "100"))
        store.upsert_price_snapshot(snapshot("1", "100"))

        assert len(history_rows(store)) == 1


def test_record_price_scan_appends_history_for_changed_baselines(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot("1", "100"))
        backdate_history(store, 60)
        current = snapshot("1", "90")
        store.record_price_scan(
            feed_id="feed",
            group_id="group",
            observed_at=NOW,
            baseline_updates=(current,),
            notifications=(
                PriceNotification(
                    product_id="1",
                    previous=snapshot("1", "100"),
                    current=current,
                    entry=EntryData(
                        title="Item",
                        link="https://example.test/1",
                        description="",
                        author="",
                        timestamp=None,
                    ),
                    source_title="Shop",
                ),
            ),
        )

        assert [
            (amount, formatted) for _, amount, formatted, _ in history_rows(store)
        ] == [
            ("100", "100 MKD"),
            ("90", "90 MKD"),
        ]


def test_window_includes_the_pre_window_baseline_and_the_current_price(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "state.db")
    initialize_schema(connection)
    insert_history(connection, "1", "50", NOW - 60 * DAY)
    insert_history(connection, "1", "120", NOW - 40 * DAY)
    insert_history(connection, "1", "200", NOW - 10 * DAY)
    insert_history(connection, "1", "150", NOW - 5 * DAY)
    connection.commit()

    contexts = PriceHistoryRepository(connection).load_price_history_context(
        "feed",
        {"1": snapshot("1", "90")},
        now=NOW,
    )

    context = contexts["1"]
    assert context.low == Decimal(90)
    assert context.low_formatted == "90 MKD"
    assert context.high == Decimal(200)
    assert context.high_formatted == "200 MKD"
    assert context.is_low is True


def test_rows_older_than_the_pre_window_baseline_are_ignored(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "state.db")
    initialize_schema(connection)
    insert_history(connection, "1", "10", NOW - 90 * DAY)
    insert_history(connection, "1", "120", NOW - 40 * DAY)
    connection.commit()

    context = PriceHistoryRepository(connection).load_price_history_context(
        "feed",
        {"1": snapshot("1", "130")},
        now=NOW,
    )["1"]

    assert context.low == Decimal(120)
    assert context.low_formatted == "120 MKD"
    assert context.high == Decimal(130)
    assert context.is_low is False


def test_history_extreme_text_comes_from_the_row_that_supplied_it(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "state.db")
    initialize_schema(connection)
    insert_history(connection, "1", "100", NOW - 2 * DAY, formatted="100,00 MKD")
    insert_history(connection, "1", "160", NOW - DAY, formatted="160,00 MKD")
    connection.commit()

    context = PriceHistoryRepository(connection).load_price_history_context(
        "feed",
        {"1": snapshot("1", "120")},
        now=NOW,
    )["1"]

    assert context.low == Decimal(100)
    assert context.low_formatted == "100,00 MKD"
    assert context.high == Decimal(160)
    assert context.high_formatted == "160,00 MKD"
    assert context.is_low is False


def test_rows_in_another_currency_do_not_participate(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "state.db")
    initialize_schema(connection)
    insert_history(connection, "1", "40", NOW - DAY, currency="EUR")
    insert_history(connection, "1", "200", NOW - 2 * DAY)
    connection.commit()

    context = PriceHistoryRepository(connection).load_price_history_context(
        "feed",
        {"1": snapshot("1", "90")},
        now=NOW,
    )["1"]

    assert context.low == Decimal(90)
    assert context.high == Decimal(200)
    assert context.is_low is True


def test_the_current_observation_participates_in_the_range(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "state.db")
    initialize_schema(connection)
    insert_history(connection, "1", "100", NOW - DAY)
    connection.commit()

    context = PriceHistoryRepository(connection).load_price_history_context(
        "feed",
        {"1": snapshot("1", "80")},
        now=NOW,
    )["1"]

    assert context.low == Decimal(80)
    assert context.low_formatted == "80 MKD"
    assert context.is_low is True


def test_decimal_ordering_is_numeric_not_lexicographic(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "state.db")
    initialize_schema(connection)
    insert_history(connection, "1", "10.00", NOW - DAY)
    connection.commit()

    context = PriceHistoryRepository(connection).load_price_history_context(
        "feed",
        {"1": snapshot("1", "9.99")},
        now=NOW,
    )["1"]

    assert context.low == Decimal("9.99")
    assert context.high == Decimal("10.00")


def test_a_product_without_history_returns_no_context(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "state.db")
    initialize_schema(connection)
    connection.commit()

    contexts = PriceHistoryRepository(connection).load_price_history_context(
        "feed",
        {"1": snapshot("1", "90")},
        now=NOW,
    )

    assert contexts == {}


def test_a_row_matching_the_current_observation_only_is_not_history(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "state.db")
    initialize_schema(connection)
    insert_history(connection, "1", "90", NOW - 20)
    connection.commit()

    contexts = PriceHistoryRepository(connection).load_price_history_context(
        "feed",
        {"1": snapshot("1", "90")},
        now=NOW,
    )

    assert contexts == {}


def test_a_pre_window_row_equal_to_the_current_price_still_counts(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "state.db")
    initialize_schema(connection)
    insert_history(connection, "1", "90", NOW - 40 * DAY)
    connection.commit()

    context = PriceHistoryRepository(connection).load_price_history_context(
        "feed",
        {"1": snapshot("1", "90")},
        now=NOW,
    )["1"]

    assert context.low == Decimal(90)
    assert context.high == Decimal(90)
    assert context.is_low is True


def test_many_product_ids_are_queried_in_chunks(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "state.db")
    initialize_schema(connection)
    product_ids = tuple(str(index) for index in range(850))
    connection.executemany(
        INSERT_HISTORY,
        (
            ("feed", product_id, "100", "100 MKD", "MKD", NOW - DAY)
            for product_id in product_ids
        ),
    )
    connection.commit()

    contexts = PriceHistoryRepository(connection).load_price_history_context(
        "feed",
        {product_id: snapshot(product_id, "90") for product_id in product_ids},
        now=NOW,
    )

    assert len(contexts) == len(product_ids)
    assert all(context.is_low for context in contexts.values())
    assert all(context.high == Decimal(100) for context in contexts.values())
