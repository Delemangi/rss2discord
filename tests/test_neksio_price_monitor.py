from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import PriceDirection, SourceMetric
from rss2discord.transports import FeedFetchError, neksio_price_monitor
from tests.discord_components_helpers import get_text_display_contents
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


def test_scan_rejects_snapshot_history_past_the_retention_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first = make_product(1, amount="100", formatted="100 MKD")
    second = make_product(2, amount="200", formatted="200 MKD")
    monkeypatch.setattr(
        neksio_price_monitor,
        "MAX_NEKSIO_RETAINED_SNAPSHOTS",
        1,
        raising=False,
    )

    # When / Then
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(first,), (second,)]),
            store,
            RecordingSender([]),
        )
        monitor.scan()
        with pytest.raises(FeedFetchError, match="SnapshotLimitExceeded"):
            monitor.scan()
        assert set(snapshots_by_product(store)) == {1}


def test_scan_treats_equal_decimal_prices_as_formatting_only_changes(
    tmp_path: Path,
) -> None:
    baseline = make_product(1, amount="1.20", formatted="1,20 MKD")
    refreshed = make_product(1, amount="1.2", formatted="1.20 MKD")

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(baseline,), (refreshed,)]),
            store,
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
        "Product 30",
        "Product 10",
    ]
    assert [message.entry.description for message in sender.messages] == ["", ""]
    assert [message.entry.price_direction for message in sender.messages] == [
        PriceDirection.DECREASE,
        PriceDirection.INCREASE,
    ]
    # The deleted sentence used to carry both prices, so every alert must still
    # expose its own headline price and exactly one prior price.
    assert [message.entry.source_metrics[0] for message in sender.messages] == [
        SourceMetric(label="Price", value="90 MKD"),
        SourceMetric(label="Price", value="110 MKD"),
    ]
    assert [
        [
            metric
            for metric in message.entry.source_metrics
            if metric.label == "Previous"
        ]
        for message in sender.messages
    ] == [
        [SourceMetric(label="Previous", value="100 MKD", prior=True)],
        [SourceMetric(label="Previous", value="100 MKD", prior=True)],
    ]
    entry = sender.messages[0].entry
    assert entry.link == "https://g.store.neksio.mk/Product/Details/30"
    assert entry.image_url == "https://g.store.neksio.mk/images/30.jpg"
    assert entry.categories == ("Laptops", "Gaming")
    assert entry.timestamp == "2026-07-26T12:00:00+00:00"
    assert entry.source_metrics == (
        SourceMetric(label="Price", value="90 MKD"),
        SourceMetric(label="Previous", value="100 MKD", prior=True),
        SourceMetric(label="Original", value="150 MKD"),
        SourceMetric(label="Product code", value="CODE-30"),
        SourceMetric(label="Manufacturer", value="Neksio"),
        SourceMetric(label="Stock", value="3"),
    )


def test_scan_keeps_hostile_formatted_prices_inert_and_omits_empty_subcategory(
    tmp_path: Path,
) -> None:
    # Given
    before = make_product(
        1,
        amount="100",
        formatted="[old](https://evil.example)",
        subcategory="",
    )
    after = make_product(
        1,
        amount="90",
        formatted="[new](https://evil.example)",
        subcategory="",
    )
    sender = RecordingSender([True])

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(before,), (after,)]),
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()

    # Then
    entry = sender.messages[0].entry
    assert entry.description == ""
    assert entry.price_direction == PriceDirection.DECREASE
    assert entry.source_metrics[:2] == (
        SourceMetric(label="Price", value="[new](https://evil.example)"),
        SourceMetric(label="Previous", value="[old](https://evil.example)", prior=True),
    )
    assert all(
        "](https://evil.example)" not in content
        for content in get_text_display_contents(sender.messages[0])
    )
    assert entry.categories == ("Laptops",)


def test_failed_delivery_retries_and_persists_after_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    baseline = make_product(1, amount="100", formatted="100 MKD")
    changed = make_product(1, amount="90", formatted="90 MKD")
    sender = RecordingSender([False, True])

    with DeliveryStore(database_path) as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(baseline,), (changed,), (changed,)]),
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        assert snapshots_by_product(store)[1].formatted == "100 MKD"
        monitor.scan()
        assert snapshots_by_product(store)[1].formatted == "90 MKD"

    with DeliveryStore(database_path) as store:
        reopened_sender = RecordingSender([])
        make_monitor(
            make_feed(),
            CatalogStub([(changed,)]),
            store,
            reopened_sender,
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
        "Product 1",
        "Product 3",
    ]
    assert snapshots[1].formatted == "100 MKD"
    assert snapshots[3].formatted == "290 MKD"


def test_scan_delays_only_after_accepted_alerts(tmp_path: Path) -> None:
    before = tuple(
        make_product(
            product_id,
            amount=str(product_id * 100),
            formatted=f"{product_id}00 MKD",
        )
        for product_id in (1, 2, 3)
    )
    after = tuple(
        make_product(
            product_id,
            amount=str(product_id * 100 - 1),
            formatted=f"{product_id}99 MKD",
        )
        for product_id in (1, 2, 3)
    )
    delays: list[float] = []

    def record_delay(seconds: float) -> bool:
        delays.append(seconds)
        return True

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([before, after]),
            store,
            RecordingSender([True, False, True]),
            sleep=record_delay,
            delay_between_posts=2.5,
        )
        monitor.scan()
        monitor.scan()

    assert delays == [2.5]
