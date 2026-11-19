import time
from decimal import Decimal
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import EntryData, PriceSnapshot
from rss2discord.price_notifications import PriceNotification

RETENTION_DAYS = 90
OLD = int(time.time()) - (RETENTION_DAYS + 30) * 86_400


def _snapshot(product_id: str) -> PriceSnapshot:
    return PriceSnapshot(
        feed_id="feed",
        product_id=product_id,
        amount=Decimal(90),
        formatted="90 MKD",
        currency="MKD",
    )


def _notification(product_id: str) -> PriceNotification:
    return PriceNotification(
        product_id=product_id,
        previous=_snapshot(product_id),
        current=_snapshot(product_id),
        entry=EntryData(
            title=f"Item {product_id}",
            link=f"https://example.test/{product_id}",
            description="",
            author="",
            timestamp=None,
        ),
        source_title="Shop",
    )


def test_prune_old_records_keeps_recent_and_pending(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.mark_delivered_many("feed", ("old", "new"))
        with store._connection:
            store._connection.execute(
                "UPDATE delivered_entries SET delivered_at = ? WHERE entry_id = 'old'",
                (OLD,),
            )

        for product_id in ("1", "2", "3"):
            store.record_price_scan(
                feed_id="feed",
                group_id=f"group-{product_id}",
                observed_at=OLD,
                baseline_updates=(),
                notifications=(_notification(product_id),),
            )
        notification_ids = [
            int(row[0])
            for row in store._connection.execute(
                "SELECT notification_id FROM price_notifications "
                "ORDER BY notification_id",
            )
        ]
        store.mark_price_notifications_sent(notification_ids[:2])
        with store._connection:
            store._connection.execute(
                "UPDATE price_notifications SET sent_at = ? WHERE notification_id = ?",
                (OLD, notification_ids[0]),
            )

        result = store.prune_old_records(retention_days=RETENTION_DAYS)

        assert result.delivered_entries == 1
        assert result.price_notifications == 1
        assert result.total == 2
        assert store.has_delivered("feed", "new")
        assert not store.has_delivered("feed", "old")
        assert store.count_pending_price_notifications("feed") == 1
        remaining = store.list_price_notifications("feed")
        assert len(remaining) == 2
        assert {str(row["status"]) for row in remaining} == {"pending", "sent"}


def test_prune_old_records_disabled_for_zero_days(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.mark_delivered_many("feed", ("old",))
        with store._connection:
            store._connection.execute(
                "UPDATE delivered_entries SET delivered_at = ?",
                (OLD,),
            )

        result = store.prune_old_records(retention_days=0)

        assert result.total == 0
        assert store.has_delivered("feed", "old")
