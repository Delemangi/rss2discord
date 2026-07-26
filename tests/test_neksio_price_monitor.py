from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import SourceMetric
from rss2discord.retries import FeedFetchInterruptedError
from tests.neksio_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
    make_feed,
    make_monitor,
    make_product,
    snapshots_by_product,
)


def test_scan_baselines_new_products_silently_and_keeps_removed_history(
    tmp_path: Path,
) -> None:
    first = make_product(1, amount="100", formatted="100 MKD")
    second = make_product(2, amount="200", formatted="200 MKD")
    catalog = CatalogStub([(first,), (first, second), (first,)])
    sender = RecordingSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()
        monitor.scan()

        assert sender.messages == []
        assert set(snapshots_by_product(store)) == {1, 2}


def test_scan_treats_equal_decimal_prices_as_formatting_only_changes(
    tmp_path: Path,
) -> None:
    baseline = make_product(1, amount="1.20", formatted="1,20 MKD")
    refreshed = make_product(1, amount="1.2", formatted="1.20 MKD")

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(), CatalogStub([(baseline,), (refreshed,)]), store,
            RecordingSender([]),
        )
        monitor.scan()
        monitor.scan()

        snapshot = snapshots_by_product(store)[1]
        assert snapshot.amount == Decimal("1.2")
        assert snapshot.formatted == "1.20 MKD"


def test_scan_delivers_in_catalog_order_with_neksio_metadata(
    tmp_path: Path,
) -> None:
    decrease_before = make_product(30, amount="100", formatted="100 MKD")
    increase_before = make_product(10, amount="100", formatted="100 MKD")
    decrease_after = make_product(30, amount="90", formatted="90 MKD")
    increase_after = make_product(10, amount="110", formatted="110 MKD")
    sender = RecordingSender([True, True])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub(
                [
                    (decrease_before, increase_before),
                    (decrease_after, increase_after),
                ],
            ),
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()

    assert [message.entry.title for message in sender.messages] == [
        "Product 30", "Product 10",
    ]
    assert [message.entry.description for message in sender.messages] == [
        "Price decreased from 100 MKD to 90 MKD",
        "Price increased from 100 MKD to 110 MKD",
    ]
    entry = sender.messages[0].entry
    assert entry.link == "https://g.store.neksio.mk/Product/Details/30"
    assert entry.image_url == "https://g.store.neksio.mk/images/30.jpg"
    assert entry.categories == ("Laptops", "Gaming")
    assert entry.timestamp == "2026-07-26T12:00:00+00:00"
    assert entry.source_metrics == (
        SourceMetric(label="Price", value="90 MKD"),
        SourceMetric(label="Previous", value="100 MKD"),
        SourceMetric(label="Original", value="150 MKD"),
        SourceMetric(label="Product code", value="CODE-30"),
        SourceMetric(label="Manufacturer", value="Neksio"),
        SourceMetric(label="Stock", value="3"),
    )


def test_failed_delivery_retries_and_persists_after_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    baseline = make_product(1, amount="100", formatted="100 MKD")
    changed = make_product(1, amount="90", formatted="90 MKD")
    sender = RecordingSender([False, True])

    with DeliveryStore(database_path) as store:
        monitor = make_monitor(
            make_feed(), CatalogStub([(baseline,), (changed,), (changed,)]),
            store, sender,
        )
        monitor.scan()
        monitor.scan()
        assert snapshots_by_product(store)[1].formatted == "100 MKD"
        monitor.scan()
        assert snapshots_by_product(store)[1].formatted == "90 MKD"

    with DeliveryStore(database_path) as store:
        reopened_sender = RecordingSender([])
        make_monitor(
            make_feed(), CatalogStub([(changed,)]), store, reopened_sender,
        ).scan()
        assert reopened_sender.messages == []


def test_failed_delivery_does_not_suppress_later_changes(tmp_path: Path) -> None:
    first_before = make_product(1, amount="100", formatted="100 MKD")
    later_before = make_product(3, amount="300", formatted="300 MKD")
    first_after = make_product(1, amount="90", formatted="90 MKD")
    later_after = make_product(3, amount="290", formatted="290 MKD")
    sender = RecordingSender([False, True])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub(
                [(first_before, later_before), (first_after, later_after)],
            ),
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        snapshots = snapshots_by_product(store)

    assert [message.entry.title for message in sender.messages] == [
        "Product 1", "Product 3",
    ]
    assert snapshots[1].formatted == "100 MKD"
    assert snapshots[3].formatted == "290 MKD"


def test_scan_delays_only_after_accepted_alerts(tmp_path: Path) -> None:
    before = tuple(
        make_product(product_id, amount=str(product_id * 100), formatted=f"{product_id}00 MKD")
        for product_id in (1, 2, 3)
    )
    after = tuple(
        make_product(product_id, amount=str(product_id * 100 - 1), formatted=f"{product_id}99 MKD")
        for product_id in (1, 2, 3)
    )
    delays: list[float] = []

    def record_delay(seconds: float) -> bool:
        delays.append(seconds)
        return True

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(), CatalogStub([before, after]), store,
            RecordingSender([True, False, True]),
            sleep=record_delay,
            delay_between_posts=2.5,
        )
        monitor.scan()
        monitor.scan()

    assert delays == [2.5]


def test_shutdown_before_fetch_stops_without_loading_snapshots(tmp_path: Path) -> None:
    sender = RecordingSender([])
    fetch_calls: list[str] = []
    catalog = CatalogStub([()])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(), catalog, store, sender,
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
        baseline = make_monitor(make_feed(), CatalogStub([(before,)]), store, RecordingSender([]))
        baseline.scan()
        with pytest.raises(FeedFetchInterruptedError):
            make_monitor(
                make_feed(), CatalogStub([(after,)]), store, sender,
                is_shutdown_requested=shutdown_after_fetch,
            ).scan()
        assert sender.messages == []
        assert snapshots_by_product(store)[1].formatted == "100 MKD"


def test_interrupted_delivery_stops_later_alerts_and_preserves_snapshot(
    tmp_path: Path,
) -> None:
    before = tuple(
        make_product(product_id, amount=str(product_id * 100), formatted=f"{product_id}00 MKD")
        for product_id in (1, 2)
    )
    after = tuple(
        make_product(product_id, amount=str(product_id * 100 - 1), formatted=f"{product_id}99 MKD")
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
