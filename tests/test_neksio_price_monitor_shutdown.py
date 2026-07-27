from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.retries import FeedFetchInterruptedError
from tests.neksio_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
    make_feed,
    make_monitor,
    make_product,
    snapshots_by_product,
)


def test_shutdown_before_fetch_stops_without_loading_snapshots(tmp_path: Path) -> None:
    sender = RecordingSender([])
    fetch_calls: list[str] = []
    catalog = CatalogStub([()])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            catalog,
            store,
            sender,
            is_shutdown_requested=lambda: True,
        )
        with pytest.raises(FeedFetchInterruptedError):
            monitor.scan()
        fetch_calls.extend(catalog.urls)

    assert fetch_calls == []
    assert sender.messages == []


def test_scan_passes_the_runtime_shutdown_callback_to_the_catalog(
    tmp_path: Path,
) -> None:
    # Given
    catalog = CatalogStub([()])

    def is_shutdown_requested() -> bool:
        return False

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        make_monitor(
            make_feed(),
            catalog,
            store,
            RecordingSender([]),
            is_shutdown_requested=is_shutdown_requested,
        ).scan()

    # Then
    assert catalog.shutdown_callbacks == [is_shutdown_requested]


def test_shutdown_after_fetch_stops_before_delivery(tmp_path: Path) -> None:
    checks = 0

    def shutdown_after_fetch() -> bool:
        nonlocal checks
        checks += 1
        return checks == 2

    before = make_product(1, amount="100", formatted="100 MKD")
    after = make_product(1, amount="90", formatted="90 MKD")
    sender = RecordingSender([True])

    with DeliveryStore(tmp_path / "state.db") as store:
        baseline = make_monitor(
            make_feed(),
            CatalogStub([(before,)]),
            store,
            RecordingSender([]),
        )
        baseline.scan()
        with pytest.raises(FeedFetchInterruptedError):
            make_monitor(
                make_feed(),
                CatalogStub([(after,)]),
                store,
                sender,
                is_shutdown_requested=shutdown_after_fetch,
            ).scan()
        assert sender.messages == []
        assert snapshots_by_product(store)[1].formatted == "100 MKD"


def test_interrupted_delivery_stops_later_alerts_and_preserves_snapshot(
    tmp_path: Path,
) -> None:
    before = tuple(
        make_product(
            product_id,
            amount=str(product_id * 100),
            formatted=f"{product_id}00 MKD",
        )
        for product_id in (1, 2)
    )
    after = tuple(
        make_product(
            product_id,
            amount=str(product_id * 100 - 1),
            formatted=f"{product_id}99 MKD",
        )
        for product_id in (1, 2)
    )
    sender = RecordingSender([DiscordDeliveryResult.INTERRUPTED])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), CatalogStub([before, after]), store, sender)
        monitor.scan()
        monitor.scan()
        snapshots = snapshots_by_product(store)

    assert [message.entry.title for message in sender.messages] == ["Product 1"]
    assert snapshots[1].formatted == "100 MKD"
    assert snapshots[2].formatted == "200 MKD"
