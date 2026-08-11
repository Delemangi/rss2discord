from collections.abc import Callable
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import PriceDirection, SourceMetric
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports import FeedFetchError, hivetec_price_monitor
from rss2discord.transports.hivetec_models import HivetecProduct
from rss2discord.transports.hivetec_price_monitor import (
    HivetecPriceMonitor,
    HivetecPriceMonitorDependencies,
)
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.hivetec_helpers import SHOP_URL, product_payload
from tests.setec_price_monitor_helpers import RecordingSender


class CatalogStub:
    def __init__(self, batches: list[tuple[HivetecProduct, ...]]) -> None:
        self._batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[HivetecProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._batches.pop(0)


def product(
    product_id: int,
    price: str,
    regular_price: str = "199900",
) -> HivetecProduct:
    return HivetecProduct.model_validate(
        product_payload(product_id, price=price, regular_price=regular_price),
    )


def feed() -> FeedConfig:
    return FeedConfig(
        id="hivetec",
        name="Hivetec Products",
        url=SHOP_URL,
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="hivetec",
    )


def monitor(
    catalog: CatalogStub,
    store: DeliveryStore,
    sender: RecordingSender,
) -> HivetecPriceMonitor:
    return HivetecPriceMonitor(
        feed(),
        HivetecPriceMonitorDependencies(
            catalog=catalog,
            snapshots=store,
            sender=sender,
            fetch_retry_policy=FetchRetryPolicy(
                sleep=lambda seconds: True,
                on_retry=lambda error, delay: None,
            ),
            sqlite_retry_policy=SQLiteRetryPolicy(
                sleep=lambda seconds: True,
                on_retry=lambda error, delay: None,
            ),
            delivery=PriceAlertDelivery(
                sleep=lambda seconds: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
        ),
    )


def test_hivetec_price_monitor_baselines_then_delivers_price_change(
    tmp_path: Path,
) -> None:
    # Given
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    catalog = CatalogStub([(product(1, "149900"),), (product(1, "129900"),)])

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(catalog, store, sender)

        # When
        price_monitor.scan()
        price_monitor.scan()

        # Then
        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert message.entry.description == ""
        assert message.entry.price_direction is PriceDirection.DECREASE
        assert message.entry.link == "https://hivetec.mk/product/product-1/"
        assert [metric.label for metric in message.entry.source_metrics] == [
            "Price",
            "Previous",
            "Original",
            "Stock",
            "SKU",
        ]
        assert message.entry.source_metrics[:2] == (
            SourceMetric("Price", "1.299 ден."),
            SourceMetric("Previous", "1.499 ден.", prior=True),
        )
        assert store.load_price_snapshots("hivetec")[0].amount.as_tuple().digits == (
            1,
            2,
            9,
            9,
        )


def test_hivetec_price_monitor_retries_failed_delivery_without_advancing_snapshot(
    tmp_path: Path,
) -> None:
    # Given
    sender = RecordingSender(
        [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.DELIVERED],
    )
    changed = (product(1, "129900"),)
    catalog = CatalogStub([(product(1, "149900"),), changed, changed])

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(catalog, store, sender)

        # When
        price_monitor.scan()
        price_monitor.scan()
        amount_after_failure = store.load_price_snapshots("hivetec")[0].amount
        price_monitor.scan()

        # Then
        assert str(amount_after_failure) == "1499"
        assert str(store.load_price_snapshots("hivetec")[0].amount) == "1299"
        assert len(sender.messages) == 2


def test_hivetec_price_monitor_rejects_change_limit_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(hivetec_price_monitor, "MAX_HIVETEC_PRICE_CHANGES_PER_SCAN", 1)
    baseline = (product(1, "149900"), product(2, "249900", "299900"))
    changed = (product(1, "129900"), product(2, "229900", "299900"))

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(
            CatalogStub([baseline, changed]),
            store,
            RecordingSender([]),
        )
        price_monitor.scan()

        # When / Then
        with pytest.raises(FeedFetchError, match="PriceChangeLimitExceeded"):
            price_monitor.scan()
        assert {
            str(snapshot.amount) for snapshot in store.load_price_snapshots("hivetec")
        } == {
            "1499",
            "2499",
        }


def test_hivetec_price_monitor_preserves_snapshot_when_price_is_zero(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(
            CatalogStub([(product(1, "149900"),), (product(1, "0"),)]),
            store,
            RecordingSender([]),
        )

        price_monitor.scan()
        price_monitor.scan()

        assert str(store.load_price_snapshots("hivetec")[0].amount) == "1499"


def test_hivetec_price_monitor_rejects_snapshot_limit_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hivetec_price_monitor, "MAX_HIVETEC_RETAINED_SNAPSHOTS", 1)

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(
            CatalogStub([(product(1, "149900"), product(2, "249900", "299900"))]),
            store,
            RecordingSender([]),
        )

        with pytest.raises(FeedFetchError, match="SnapshotLimitExceeded"):
            price_monitor.scan()
        assert store.load_price_snapshots("hivetec") == ()
