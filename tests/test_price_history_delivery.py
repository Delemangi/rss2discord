"""Inline price-change alerts carry 30-day history context."""

import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import EntryData, PriceSnapshot
from rss2discord.price_notifications import (
    PriceNotification,
    PriceNotificationDeliveryResult,
    entry_from_json,
    entry_to_json,
)
from rss2discord.retries import SQLiteRetryPolicy
from rss2discord.transports.price_monitor import PriceAlertDelivery, PriceDeliveryPlan
from rss2discord.transports.price_monitor.delivery import (
    deliver_price_changes,
    deliver_price_notifications,
)
from tests.setec_price_monitor_helpers import RecordingSender

DAY = 86_400
NOW = int(time.time())

INSERT_HISTORY = (
    "INSERT INTO price_history "
    "(feed_id, product_id, amount, formatted, currency, observed_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def feed(*, price_inline_limit: int = 10) -> FeedConfig:
    return FeedConfig(
        id="feed",
        url="https://shop.example.test/feed",
        webhook="https://discord.example.test/hook",
        strategy="anhoch",
        price_inline_limit=price_inline_limit,
    )


def snapshot(product_id: str, amount: str) -> PriceSnapshot:
    return PriceSnapshot("feed", product_id, Decimal(amount), f"{amount} MKD", "MKD")


def entry(*, percentage: str | None = "-10%") -> EntryData:
    return EntryData(
        title="Product 1",
        link="https://shop.example.test/1",
        description="",
        author="Shop",
        timestamp=None,
        price_change_percentage=percentage,
    )


def change(
    product_id: str,
    amount: str,
    previous_amount: str = "100",
) -> SimpleNamespace:
    return SimpleNamespace(
        previous=snapshot(product_id, previous_amount),
        current=snapshot(product_id, amount),
    )


def message_for(_change: SimpleNamespace) -> WebhookMessage:
    return WebhookMessage(feed=feed(), entry=entry(), source_title="Shop")


def seed_history(
    store: DeliveryStore,
    product_id: str,
    *rows: tuple[str, int],
) -> None:
    with store._connection:
        store._connection.executemany(
            INSERT_HISTORY,
            (
                (
                    "feed",
                    product_id,
                    amount,
                    f"{amount} MKD",
                    "MKD",
                    NOW - days_ago * DAY,
                )
                for amount, days_ago in rows
            ),
        )


def dependencies(
    store: DeliveryStore,
    sender: RecordingSender,
) -> SimpleNamespace:
    return SimpleNamespace(
        snapshots=store,
        sender=sender,
        sqlite_retry_policy=SQLiteRetryPolicy(
            sleep=lambda _: True,
            on_retry=lambda *_: None,
        ),
        delivery=PriceAlertDelivery(
            sleep=lambda _: True,
            delay_between_posts=0,
            is_shutdown_requested=lambda: False,
        ),
    )


def test_direct_inline_alert_receives_history_context(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        seed_history(store, "1", ("250", 5), ("200", 10))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        deliver_price_changes(
            [change("1", "90")],
            dependencies(store, sender),
            message_for,
            plan=PriceDeliveryPlan(("1",)),
        )

        assert len(sender.messages) == 1
        rendered = sender.messages[0].entry
        assert rendered.price_period_low == "90 MKD"
        assert rendered.price_period_high == "250 MKD"
        assert rendered.price_period_days == 30
        assert rendered.price_is_period_low is True
        assert store.load_price_snapshots("feed")[0].amount == Decimal(90)


def test_direct_inline_alert_above_the_range_is_not_a_low(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        seed_history(store, "1", ("100", 5), ("80", 10))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        deliver_price_changes(
            [change("1", "300")],
            dependencies(store, sender),
            message_for,
            plan=PriceDeliveryPlan(("1",)),
        )

        rendered = sender.messages[0].entry
        assert rendered.price_period_low == "80 MKD"
        assert rendered.price_period_high == "300 MKD"
        assert rendered.price_is_period_low is False


def test_pending_outbox_inline_card_receives_history_context(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        seed_history(store, "1", ("250", 5), ("200", 10))
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
                    entry=entry(),
                    source_title="Shop",
                ),
            ),
        )
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        result = deliver_price_notifications(
            dependencies(store, sender),
            feed(),
            "feed",
        )

        assert result is PriceNotificationDeliveryResult.DRAINED
        assert len(sender.messages) == 1
        rendered = sender.messages[0].entry
        assert rendered.price_period_low == "90 MKD"
        assert rendered.price_period_high == "250 MKD"
        assert rendered.price_period_days == 30
        assert rendered.price_is_period_low is True


def test_bulk_digest_messages_are_not_enriched(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        for product_id in ("1", "2", "3"):
            seed_history(store, product_id, ("250", 5))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        deliver_price_changes(
            [change(product_id, "90") for product_id in ("1", "2", "3")],
            dependencies(store, sender),
            message_for,
            plan=PriceDeliveryPlan(bulk=True),
            feed=feed(price_inline_limit=1),
        )

        assert len(sender.messages) == 1
        digest_entry = sender.messages[0].entry
        assert digest_entry.price_period_low is None
        assert digest_entry.price_period_high is None
        assert digest_entry.price_is_period_low is False


def test_entry_without_a_change_percentage_is_not_enriched(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        seed_history(store, "1", ("250", 5))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        deliver_price_changes(
            [change("1", "90")],
            dependencies(store, sender),
            lambda _change: WebhookMessage(
                feed=feed(),
                entry=entry(percentage=None),
                source_title="Shop",
            ),
            plan=PriceDeliveryPlan(("1",)),
        )

        rendered = sender.messages[0].entry
        assert rendered.price_period_low is None
        assert rendered.price_period_high is None


def test_history_fields_round_trip_through_the_outbox_json() -> None:
    original = replace(
        entry(),
        price_period_low="90 MKD",
        price_period_high="250 MKD",
        price_period_days=30,
        price_is_period_low=True,
    )

    assert entry_from_json(entry_to_json(original)) == original
