import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from rss2discord.delivery_notifications import MAX_PRICE_NOTIFICATION_ATTEMPTS
from rss2discord.delivery_store import DeliveryStore
from rss2discord.health_report import build_health_summary, format_health_summary
from rss2discord.models import EntryData, PriceSnapshot
from rss2discord.price_notifications import PriceNotification


def _notification(product_id: str, *, feed_id: str = "feed") -> PriceNotification:
    previous = PriceSnapshot(
        feed_id,
        product_id,
        Decimal(100),
        "100 MKD",
        "MKD",
    )
    current = PriceSnapshot(
        feed_id,
        product_id,
        Decimal(90),
        "90 MKD",
        "MKD",
    )
    return PriceNotification(
        product_id=product_id,
        previous=previous,
        current=current,
        entry=EntryData(
            title=f"Product {product_id}",
            link=f"https://shop.example.test/{product_id}",
            description="",
            author="Shop",
            timestamp=None,
        ),
        source_title="Test Shop",
    )


def _seed_group(
    store: DeliveryStore,
    *,
    group_id: str,
    product_id: str,
    feed_id: str = "feed",
) -> None:
    notification = _notification(product_id, feed_id=feed_id)
    store.record_price_scan(
        feed_id=feed_id,
        group_id=group_id,
        observed_at=1,
        baseline_updates=(notification.current,),
        notifications=(notification,),
    )


def _ids(store: DeliveryStore, feed_id: str = "feed") -> tuple[int, ...]:
    group = store.load_pending_price_group(feed_id)
    assert group is not None
    return tuple(record.notification_id for record in group[1])


def test_attempts_below_the_limit_keep_the_group_pending(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        _seed_group(store, group_id="group-a", product_id="1")
        ids = _ids(store)

        for _ in range(MAX_PRICE_NOTIFICATION_ATTEMPTS - 1):
            store.record_price_notification_attempt(ids, "DiscordDeliveryFailed")

        assert store.count_pending_price_notifications("feed") == 1
        assert store.count_dead_lettered_price_notifications("feed") == 0
        group = store.load_pending_price_group("feed")
        assert group is not None
        assert group[1][0].attempts == MAX_PRICE_NOTIFICATION_ATTEMPTS - 1


def test_limit_dead_letters_the_group_and_reveals_the_next_one(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        _seed_group(store, group_id="group-a", product_id="1")
        _seed_group(store, group_id="group-b", product_id="2")
        first_ids = _ids(store)

        for _ in range(MAX_PRICE_NOTIFICATION_ATTEMPTS):
            store.record_price_notification_attempt(
                first_ids,
                "DiscordDeliveryFailed",
            )

        assert store.count_pending_price_notifications("feed") == 1
        assert store.count_dead_lettered_price_notifications("feed") == 1
        next_group = store.load_pending_price_group("feed")
        assert next_group is not None
        assert next_group[0] == "group-b"

        rows = store.list_price_notifications(status="dead_lettered")
        assert len(rows) == 1
        assert rows[0]["status"] == "dead_lettered"
        assert rows[0]["attempts"] == MAX_PRICE_NOTIFICATION_ATTEMPTS
        assert rows[0]["dead_lettered_at"] is not None

        payload = store._connection.execute(
            "SELECT entry_json FROM price_notifications WHERE group_id = 'group-a'",
        ).fetchone()
        assert payload == ("{}",)


def test_dead_lettered_rows_are_not_marked_sent(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        _seed_group(store, group_id="group-a", product_id="1")
        ids = _ids(store)
        for _ in range(MAX_PRICE_NOTIFICATION_ATTEMPTS):
            store.record_price_notification_attempt(ids, "DiscordDeliveryFailed")

        store.mark_price_notifications_sent(ids)

        rows = store.list_price_notifications(status="dead_lettered")
        assert len(rows) == 1
        assert rows[0]["status"] == "dead_lettered"
        assert store.count_pending_price_notifications("feed") == 0


def test_retention_removes_old_dead_letters(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        _seed_group(store, group_id="group-a", product_id="1")
        ids = _ids(store)
        for _ in range(MAX_PRICE_NOTIFICATION_ATTEMPTS):
            store.record_price_notification_attempt(ids, "DiscordDeliveryFailed")

        old = int(time.time()) - 91 * 86_400
        store._connection.execute(
            "UPDATE price_notifications SET dead_lettered_at = ?",
            (old,),
        )
        store._connection.commit()

        result = store.prune_old_records(retention_days=90)

        assert result.price_notifications == 1
        assert store.count_dead_lettered_price_notifications("feed") == 0


def test_health_summary_surfaces_dead_letters(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        _seed_group(store, group_id="group-a", product_id="1")
        ids = _ids(store)
        for _ in range(MAX_PRICE_NOTIFICATION_ATTEMPTS):
            store.record_price_notification_attempt(ids, "DiscordDeliveryFailed")

        summary = build_health_summary(store, database)

    assert summary.dead_lettered_price_notifications == 1
    assert "Dead-lettered price notifications: 1" in format_health_summary(summary)


def test_schema_migration_adds_the_dead_letter_column(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE price_notifications ("
            "notification_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "feed_id TEXT NOT NULL, group_id TEXT NOT NULL, "
            "product_id TEXT NOT NULL, title TEXT NOT NULL, link TEXT NOT NULL, "
            "previous_price TEXT, current_price TEXT NOT NULL, "
            "currency TEXT NOT NULL, entry_json TEXT NOT NULL, "
            "source_title TEXT NOT NULL, observed_at INTEGER NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'pending', "
            "attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at INTEGER, "
            "sent_at INTEGER, last_error TEXT)",
        )

    with DeliveryStore(database) as store:
        columns = {
            str(row[1])
            for row in store._connection.execute(
                "PRAGMA table_info(price_notifications)",
            )
        }

    assert "dead_lettered_at" in columns
