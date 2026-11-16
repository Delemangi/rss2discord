"""Validation and legacy-row branches of the notification outbox."""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from rss2discord.delivery_notifications import (
    PriceNotificationRepository,
    sanitize_price_notification_error,
)
from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import EntryData, PriceSnapshot
from rss2discord.price_notifications import PriceNotification


def _snapshot(
    product_id: str = "1",
    amount: int = 90,
    feed_id: str = "feed",
) -> PriceSnapshot:
    return PriceSnapshot(
        feed_id=feed_id,
        product_id=product_id,
        amount=Decimal(amount),
        formatted=f"{amount} MKD",
        currency="MKD",
    )


def _entry(product_id: str = "1") -> EntryData:
    return EntryData(
        title=f"Item {product_id}",
        link=f"https://example.test/{product_id}",
        description="",
        author="",
        timestamp=None,
    )


def _notification(
    product_id: str = "1",
    *,
    current_feed: str = "feed",
    previous_feed: str = "feed",
    notification_product_id: str | None = None,
) -> PriceNotification:
    return PriceNotification(
        product_id=notification_product_id or product_id,
        previous=_snapshot(product_id, 100, previous_feed),
        current=_snapshot(product_id, 90, current_feed),
        entry=_entry(product_id),
        source_title="Shop",
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DeliveryStore]:
    with DeliveryStore(tmp_path / "state.db") as value:
        yield value


def test_sanitize_none_error() -> None:
    assert sanitize_price_notification_error(None) is None


def test_record_price_scan_rejects_empty_group(store: DeliveryStore) -> None:
    with pytest.raises(ValueError, match="notification group ID must be non-empty"):
        store.record_price_scan(
            feed_id="feed",
            group_id="",
            observed_at=1,
            baseline_updates=(),
            notifications=(),
        )


def test_record_price_scan_rejects_baseline_feed_mismatch(
    store: DeliveryStore,
) -> None:
    with pytest.raises(ValueError, match="baseline feed mismatch"):
        store.record_price_scan(
            feed_id="feed",
            group_id="group",
            observed_at=1,
            baseline_updates=(_snapshot(feed_id="other"),),
            notifications=(),
        )


def test_record_price_scan_rejects_notification_feed_mismatch(
    store: DeliveryStore,
) -> None:
    with pytest.raises(ValueError, match="notification feed mismatch"):
        store.record_price_scan(
            feed_id="feed",
            group_id="group",
            observed_at=1,
            baseline_updates=(),
            notifications=(_notification(current_feed="other"),),
        )


def test_record_price_scan_rejects_previous_feed_mismatch(
    store: DeliveryStore,
) -> None:
    with pytest.raises(ValueError, match="notification previous feed mismatch"):
        store.record_price_scan(
            feed_id="feed",
            group_id="group",
            observed_at=1,
            baseline_updates=(),
            notifications=(_notification(previous_feed="other"),),
        )


def test_record_price_scan_rejects_product_mismatch(store: DeliveryStore) -> None:
    with pytest.raises(ValueError, match="notification product mismatch"):
        store.record_price_scan(
            feed_id="feed",
            group_id="group",
            observed_at=1,
            baseline_updates=(),
            notifications=(_notification(notification_product_id="2"),),
        )


def test_empty_notification_id_batches_are_noops(store: DeliveryStore) -> None:
    store.mark_price_notifications_sent(())
    store.record_price_notification_attempt((), None)

    assert store.count_pending_price_notifications("feed") == 0


def test_list_price_notifications_rejects_unknown_status(
    store: DeliveryStore,
) -> None:
    with pytest.raises(ValueError, match="status must be pending, sent, or omitted"):
        store.list_price_notifications(status="unknown")


def test_confirmation_subset_rejects_nonpositive_limit(
    store: DeliveryStore,
) -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        store.select_price_confirmation_subset(
            feed_id="feed",
            product_ids=("1",),
            limit=0,
        )


def test_confirmation_subset_rejects_empty_product_id(
    store: DeliveryStore,
) -> None:
    with pytest.raises(
        ValueError,
        match="price confirmation product IDs must be non-empty",
    ):
        store.select_price_confirmation_subset(
            feed_id="feed",
            product_ids=("",),
            limit=1,
        )


def test_confirmation_subset_accepts_empty_product_ids(
    store: DeliveryStore,
) -> None:
    assert (
        store.select_price_confirmation_subset(
            feed_id="feed",
            product_ids=(),
            limit=1,
        )
        == ()
    )


def test_legacy_notification_without_current_amount_uses_previous_amount(
    store: DeliveryStore,
) -> None:
    store.record_price_scan(
        feed_id="feed",
        group_id="group",
        observed_at=1,
        baseline_updates=(_snapshot(),),
        notifications=(_notification(),),
    )
    store._connection.execute(
        "UPDATE price_notifications SET current_amount = NULL WHERE feed_id = 'feed'",
    )
    store._connection.commit()

    group = store.load_pending_price_group("feed")

    assert group is not None
    _, records = group
    assert records[0].current_amount == Decimal(100)


class _FailingConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.rolled_back = False

    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor:
        if "INSERT INTO price_confirmation_cursors" in sql:
            raise RuntimeError("boom")
        return self._connection.execute(sql, parameters)

    def __enter__(self) -> Self:
        self._connection.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self.rolled_back = True
        self._connection.rollback()


def test_confirmation_subset_rolls_back_on_database_error(
    store: DeliveryStore,
) -> None:
    wrapper = _FailingConnection(store._connection)
    repository = PriceNotificationRepository(wrapper)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="boom"):
        repository.select_price_confirmation_subset(
            feed_id="feed",
            product_ids=("1",),
            limit=1,
        )

    assert wrapper.rolled_back is True
