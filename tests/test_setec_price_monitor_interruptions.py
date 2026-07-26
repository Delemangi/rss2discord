import logging
from collections.abc import Iterable
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.retries import (
    FeedFetchInterruptedError,
    SQLiteRetryInterruptedError,
)
from tests.setec_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
    RetryingFailureCatalog,
    busy_database_error,
    make_feed,
    make_monitor,
    make_product,
)


class SnapshotStoreSpy:
    def __init__(self) -> None:
        self.load_calls = 0
        self.persisted_batches: list[tuple[PriceSnapshot, ...]] = []

    def load_price_snapshots(self, feed_id: str) -> tuple[PriceSnapshot, ...]:
        del feed_id
        self.load_calls += 1
        return ()

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None:
        self.persisted_batches.append((snapshot,))

    def upsert_price_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> None:
        self.persisted_batches.append(tuple(snapshots))


def test_shutdown_before_catalog_fetch_stops_before_snapshot_loading() -> None:
    # Given
    catalog = CatalogStub([(make_product("prod-1"),)])
    snapshots = SnapshotStoreSpy()
    monitor = make_monitor(
        make_feed(),
        catalog,
        snapshots,
        RecordingSender([]),
        is_shutdown_requested=lambda: True,
    )

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        monitor.scan()

    assert catalog.urls == []
    assert snapshots.load_calls == 0
    assert snapshots.persisted_batches == []


def test_shutdown_after_classification_skips_silent_snapshot_persistence() -> None:
    # Given
    shutdown_checks = 0
    snapshots = SnapshotStoreSpy()

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks == 3

    monitor = make_monitor(
        make_feed(),
        CatalogStub([(make_product("prod-1", calculated_amount=100),)]),
        snapshots,
        RecordingSender([]),
        is_shutdown_requested=is_shutdown_requested,
    )

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        monitor.scan()

    assert snapshots.load_calls == 1
    assert snapshots.persisted_batches == []


def test_fetch_retry_interruption_stops_before_alerting_and_logs_no_secrets(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    feed = make_feed()
    sender = RecordingSender([])

    def interrupt_retry(_seconds: float) -> bool:
        return False

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            feed,
            RetryingFailureCatalog(),
            store,
            sender,
            sleep=interrupt_retry,
        )

        # When / Then
        with caplog.at_level(logging.ERROR), pytest.raises(FeedFetchInterruptedError):
            monitor.scan()

    assert sender.messages == []
    assert feed.url not in caplog.text
    assert feed.webhook not in caplog.text


def test_sqlite_retry_interruption_stops_before_later_alerts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_before = make_product("prod-1", calculated_amount=100)
    second_before = make_product("prod-2", calculated_amount=200)
    first_after = make_product("prod-1", calculated_amount=90)
    second_after = make_product("prod-2", calculated_amount=190)
    sender = RecordingSender(
        [DiscordDeliveryResult.DELIVERED, DiscordDeliveryResult.DELIVERED],
    )

    def interrupt_retry(_seconds: float) -> bool:
        return False

    with DeliveryStore(tmp_path / "state.db") as store:
        baseline_monitor = make_monitor(
            make_feed(),
            CatalogStub([(first_before, second_before)]),
            store,
            RecordingSender([]),
        )
        baseline_monitor.scan()

        def always_busy(snapshot: PriceSnapshot) -> None:
            del snapshot
            raise busy_database_error()

        monkeypatch.setattr(store, "upsert_price_snapshot", always_busy)
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(first_after, second_after)]),
            store,
            sender,
            sleep=interrupt_retry,
        )

        # When / Then
        with pytest.raises(SQLiteRetryInterruptedError):
            monitor.scan()

    assert [message.entry.title for message in sender.messages] == ["Product prod-1"]
