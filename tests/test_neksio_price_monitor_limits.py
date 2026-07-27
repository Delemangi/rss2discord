from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.transports import FeedFetchError, neksio_price_monitor
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    before = (
        make_product(1, amount="100", formatted="100 MKD"),
        make_product(2, amount="200", formatted="200 MKD"),
    )
    after = (
        make_product(1, amount="90", formatted="90 MKD"),
        make_product(2, amount="190", formatted="190 MKD"),
        make_product(3, amount="300", formatted="300 MKD"),
    )
    monkeypatch.setattr(neksio_price_monitor, "MAX_NEKSIO_PRICE_CHANGES_PER_SCAN", 1)
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
            with pytest.raises(FeedFetchError) as error_info:
                monitor.scan()
            assert error_info.value.strategy == "Neksio"
            assert error_info.value.cause_type == "PriceChangeLimitExceeded"
        assert sender.messages == []
        assert snapshots_by_product(store) == baseline_snapshots
