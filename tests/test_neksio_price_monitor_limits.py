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


def test_scan_rejects_too_many_price_changes_before_delivery(
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
    )
    monkeypatch.setattr(neksio_price_monitor, "MAX_NEKSIO_PRICE_CHANGES_PER_SCAN", 1)
    sender = RecordingSender([True, True])

    # When / Then
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), CatalogStub([before, after]), store, sender)
        monitor.scan()
        with pytest.raises(FeedFetchError, match="PriceChangeLimitExceeded"):
            monitor.scan()
        assert sender.messages == []
        assert snapshots_by_product(store)[1].formatted == "100 MKD"
