import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord import delivery_store
from rss2discord.delivery_store import DeliveryStore


def test_delivery_store_keeps_feed_namespaces_separate(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"

    # When
    with DeliveryStore(database_path) as store:
        store.mark_delivered("feed-a", "entry-1")
        store.mark_delivered("feed-a", "entry-1")

        # Then
        assert store.has_delivered("feed-a", "entry-1")
        assert not store.has_delivered("feed-b", "entry-1")


def test_delivery_store_persists_after_reopen(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    with DeliveryStore(database_path) as store:
        store.mark_delivered("feed-a", "entry-1")

    # When
    with DeliveryStore(database_path) as reopened_store:
        delivered = reopened_store.has_delivered("feed-a", "entry-1")

    # Then
    assert delivered


def test_delivery_store_seeds_and_persists_feed_initialization(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"

    # When
    with DeliveryStore(database_path) as store:
        initialized_before_seed = store.is_feed_initialized("anhoch")
        seeded = store.seed_feed("anhoch", ("product-1", "product-2", "product-1"))
        reseeded = store.seed_feed("anhoch", ("product-3",))

        # Then
        assert not initialized_before_seed
        assert seeded
        assert not reseeded
        assert store.is_feed_initialized("anhoch")
        assert store.has_delivered("anhoch", "product-1")
        assert store.has_delivered("anhoch", "product-2")
        assert not store.has_delivered("anhoch", "product-3")
        assert not store.is_feed_initialized("other-feed")

    with DeliveryStore(database_path) as reopened_store:
        assert reopened_store.is_feed_initialized("anhoch")
        assert reopened_store.has_delivered("anhoch", "product-1")


def test_delivery_store_creates_price_snapshot_table_alongside_delivery_tables(
    tmp_path: Path,
) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"

    # When
    with DeliveryStore(database_path) as store:
        store.mark_delivered("feed-a", "entry-1")
        store.seed_feed("feed-a", ("entry-2",))
        table_names = {
            row[0]
            for row in store._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }

        # Then
        assert table_names >= {
            "anhoch_price_snapshots",
            "delivered_entries",
            "initialized_feeds",
        }
        assert store.has_delivered("feed-a", "entry-1")
        assert store.is_feed_initialized("feed-a")


def test_price_snapshots_are_isolated_by_feed_and_product(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    feed_a_snapshot = delivery_store.PriceSnapshot(
        feed_id="feed-a",
        product_id=1,
        amount=Decimal("12.50"),
        formatted="12.50 ден",
        currency="MKD",
    )
    feed_b_snapshot = delivery_store.PriceSnapshot(
        feed_id="feed-b",
        product_id=1,
        amount=Decimal("15.00"),
        formatted="15.00 ден",
        currency="MKD",
    )
    second_product = delivery_store.PriceSnapshot(
        feed_id="feed-a",
        product_id=2,
        amount=Decimal(20),
        formatted="20 ден",
        currency="MKD",
    )

    # When
    with DeliveryStore(database_path) as store:
        store.upsert_price_snapshots((feed_a_snapshot, feed_b_snapshot, second_product))
        feed_a_snapshots = store.load_price_snapshots("feed-a")
        feed_b_snapshots = store.load_price_snapshots("feed-b")

    # Then
    assert feed_a_snapshots == (feed_a_snapshot, second_product)
    assert feed_b_snapshots == (feed_b_snapshot,)


def test_price_snapshot_amounts_are_stored_as_canonical_decimal_text(
    tmp_path: Path,
) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    integer_snapshot = delivery_store.PriceSnapshot(
        feed_id="feed-a",
        product_id=1,
        amount=Decimal(42),
        formatted="42 ден",
        currency="MKD",
    )
    precise_snapshot = delivery_store.PriceSnapshot(
        feed_id="feed-a",
        product_id=2,
        amount=Decimal("123456789012.12340"),
        formatted="precise",
        currency="MKD",
    )

    # When
    with DeliveryStore(database_path) as store:
        store.upsert_price_snapshots((integer_snapshot, precise_snapshot))
        amounts = store._connection.execute(
            "SELECT amount FROM anhoch_price_snapshots ORDER BY product_id",
        ).fetchall()
        snapshots = store.load_price_snapshots("feed-a")

    # Then
    assert amounts == [
        ("42",),
        ("123456789012.1234",),
    ]
    assert snapshots == (integer_snapshot, precise_snapshot)


def test_price_snapshot_rejects_an_unsafe_exponent_before_fixed_point_formatting(
    tmp_path: Path,
) -> None:
    # Given
    snapshot = delivery_store.PriceSnapshot(
        feed_id="feed-a",
        product_id=1,
        amount=Decimal("1E+10000"),
        formatted="unsafe",
        currency="MKD",
    )

    # When / Then
    with (
        DeliveryStore(tmp_path / "rss2discord.db") as store,
        pytest.raises(
            ValueError,
            match="supported precision",
        ),
    ):
        store.upsert_price_snapshot(snapshot)


def test_price_snapshot_formatting_only_change_updates_snapshot(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    initial_snapshot = delivery_store.PriceSnapshot(
        feed_id="feed-a",
        product_id=1,
        amount=Decimal("1.2"),
        formatted="1.20 ден",
        currency="MKD",
    )
    reformatted_snapshot = delivery_store.PriceSnapshot(
        feed_id="feed-a",
        product_id=1,
        amount=Decimal("1.20"),
        formatted="1,20 ден",
        currency="MKD",
    )

    # When
    with DeliveryStore(database_path) as store:
        store.upsert_price_snapshot(initial_snapshot)
        store.upsert_price_snapshot(reformatted_snapshot)
        snapshots = store.load_price_snapshots("feed-a")

    # Then
    assert snapshots == (reformatted_snapshot,)


def test_price_snapshot_batch_upsert_rolls_back_when_one_snapshot_fails(
    tmp_path: Path,
) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    first_snapshot = delivery_store.PriceSnapshot(
        feed_id="feed-a",
        product_id=1,
        amount=Decimal(10),
        formatted="10 ден",
        currency="MKD",
    )
    rejected_snapshot = delivery_store.PriceSnapshot(
        feed_id="feed-a",
        product_id=2,
        amount=Decimal(20),
        formatted="20 ден",
        currency="MKD",
    )

    # When / Then
    with DeliveryStore(database_path) as store:
        store._connection.executescript(
            "CREATE TRIGGER reject_second_snapshot "
            "BEFORE INSERT ON anhoch_price_snapshots "
            "WHEN NEW.product_id = 2 "
            "BEGIN SELECT RAISE(ABORT, 'forced snapshot failure'); END",
        )

        with pytest.raises(sqlite3.IntegrityError, match="forced snapshot failure"):
            store.upsert_price_snapshots((first_snapshot, rejected_snapshot))

        assert store.load_price_snapshots("feed-a") == ()


def test_price_snapshot_upsert_is_a_no_op_for_unchanged_canonical_values(
    tmp_path: Path,
) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    initial_snapshot = delivery_store.PriceSnapshot(
        feed_id="feed-a",
        product_id=1,
        amount=Decimal("100.0"),
        formatted="100 ден",
        currency="MKD",
    )
    equivalent_snapshot = delivery_store.PriceSnapshot(
        feed_id="feed-a",
        product_id=1,
        amount=Decimal("100.000"),
        formatted="100 ден",
        currency="MKD",
    )

    # When
    with DeliveryStore(database_path) as store:
        store.upsert_price_snapshot(initial_snapshot)
        changes_before_repeat = store._connection.total_changes
        store.upsert_price_snapshot(equivalent_snapshot)
        changes_after_repeat = store._connection.total_changes

    # Then
    assert changes_after_repeat == changes_before_repeat


def test_price_snapshots_persist_after_reopen(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    snapshot = delivery_store.PriceSnapshot(
        feed_id="feed-a",
        product_id=1,
        amount=Decimal("9.99"),
        formatted="9.99 ден",
        currency="MKD",
    )

    # When
    with DeliveryStore(database_path) as store:
        store.upsert_price_snapshot(snapshot)

    with DeliveryStore(database_path) as reopened_store:
        snapshots = reopened_store.load_price_snapshots("feed-a")

    # Then
    assert snapshots == (snapshot,)
