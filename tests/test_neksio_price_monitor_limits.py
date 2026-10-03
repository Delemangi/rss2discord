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


def test_scan_repeatedly_rejects_too_many_price_changes_without_mutation(
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
        baseline_snapshots = snapshots_by_product(store)
        for _ in range(2):
            monitor.scan()
            assert len(store.list_price_change_batches(feed_id="neksio")) == 1
        assert sender.messages == []
        assert snapshots_by_product(store) == baseline_snapshots
