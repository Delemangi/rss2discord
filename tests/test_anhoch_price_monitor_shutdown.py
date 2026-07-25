from collections.abc import Iterable

import pytest
import requests

from rss2discord.delivery_store import PriceSnapshot
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.anhoch_catalog import AnhochCatalogClient
from tests.anhoch_helpers import (
    RecordingGet,
    StubResponse,
    page_payload,
    product_payload,
    requested_page_numbers,
)
from tests.anhoch_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
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


def test_shutdown_during_catalog_scan_skips_snapshot_loading_and_baseline_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(page_payload(1, 2, [product_payload(2, "p-2")])),
            StubResponse(page_payload(2, 2, [product_payload(1, "p-1")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)
    snapshots = SnapshotStoreSpy()
    monitor = make_monitor(
        make_feed(),
        AnhochCatalogClient(),
        snapshots,
        RecordingSender([]),
        is_shutdown_requested=lambda: len(get.urls) == 1,
    )

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        monitor.scan()

    assert requested_page_numbers(get.urls) == ["1"]
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
        CatalogStub([(make_product(1, amount="100", formatted="100 ден."),)]),
        snapshots,
        RecordingSender([]),
        is_shutdown_requested=is_shutdown_requested,
    )

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        monitor.scan()

    assert snapshots.load_calls == 1
    assert snapshots.persisted_batches == []
