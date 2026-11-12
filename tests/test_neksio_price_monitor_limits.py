from pathlib import Path

from rss2discord.delivery_store import DeliveryStore
from tests.neksio_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
    make_feed,
    make_monitor,
    make_product,
    snapshots_by_product,
)


def test_scan_summarizes_bulk_changes_then_initializes_new_products(
    tmp_path: Path,
) -> None:
    # Given
    before = tuple(
        make_product(i, amount="100", formatted="100 MKD") for i in range(1, 102)
    )
    after = (
        *(make_product(i, amount="90", formatted="90 MKD") for i in range(1, 102)),
        make_product(102, amount="300", formatted="300 MKD"),
    )
    sender = RecordingSender([True, True])

    # When / Then
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([before, after, after]),
            store,
            sender,
        )
        monitor.scan()
        for _ in range(2):
            monitor.scan()
        assert len(sender.messages) == 1
        assert sum(s.amount == 90 for s in snapshots_by_product(store).values()) == 101
