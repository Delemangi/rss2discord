from collections.abc import Callable
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import PriceDirection, SourceMetric
from rss2discord.providers.woocommerce.models import WooCommerceProduct
from rss2discord.providers.woocommerce.prices import (
    WooCommerceCatalog,
    WooCommercePriceMonitor,
)
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports import FeedFetchError
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceMonitorDependencies,
)
from tests.setec_price_monitor_helpers import RecordingSender
from tests.woocommerce_helpers import SHOP_URL, product_payload

WooCommercePriceMonitorDependencies = PriceMonitorDependencies[WooCommerceCatalog]


class CatalogStub:
    def __init__(self, batches: list[tuple[WooCommerceProduct, ...]]) -> None:
        self._batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[WooCommerceProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._batches.pop(0)


def product(
    product_id: int,
    price: str,
    regular_price: str = "199900",
) -> WooCommerceProduct:
    return WooCommerceProduct.model_validate(
        product_payload(product_id, price=price, regular_price=regular_price),
    )


def feed(*, restock_alerts: bool = False) -> FeedConfig:
    return FeedConfig(
        id="technosapien",
        name="TechnoSapien",
        url=SHOP_URL,
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="woocommerce",
        restock_alerts=restock_alerts,
    )


def monitor(
    catalog: CatalogStub,
    store: DeliveryStore,
    sender: RecordingSender,
    *,
    restock_alerts: bool = False,
) -> WooCommercePriceMonitor:
    return WooCommercePriceMonitor(
        feed(restock_alerts=restock_alerts),
        WooCommercePriceMonitorDependencies(
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


def test_woocommerce_price_monitor_baselines_then_delivers_price_change(
    tmp_path: Path,
) -> None:
    # Given
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    catalog = CatalogStub(
        [(product(1, "149900"),), (product(1, "129900"),), (product(1, "129900"),)],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(catalog, store, sender)

        # When
        price_monitor.scan()
        price_monitor.scan()
        price_monitor.scan()

        # Then
        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert message.entry.description == ""
        assert message.entry.price_direction is PriceDirection.DECREASE
        assert message.entry.discount_percentage == "35.0%"
        assert message.entry.link == "https://technosapien.mk/product/product-1/"
        assert message.entry.image_url == (
            "https://technosapien.mk/wp-content/uploads/product-1-300x300.jpg"
        )
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
        assert str(store.load_price_snapshots("technosapien")[0].amount) == "1299"
        assert store.load_price_snapshots("technosapien")[0].available is True


def test_woocommerce_price_monitor_preserves_snapshot_when_price_is_zero(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(
            CatalogStub([(product(1, "149900"),), (product(1, "0"),)]),
            store,
            RecordingSender([]),
        )

        price_monitor.scan()
        with pytest.raises(FeedFetchError, match="EmptyCatalog"):
            price_monitor.scan()

        assert str(store.load_price_snapshots("technosapien")[0].amount) == "1499"


def test_woocommerce_price_monitor_silently_baselines_out_of_stock_price(
    tmp_path: Path,
) -> None:
    out_of_stock = WooCommerceProduct.model_validate(
        product_payload(1, price="129900", in_stock=False),
    )
    with DeliveryStore(tmp_path / "state.db") as store:
        sender = RecordingSender([])
        price_monitor = monitor(
            CatalogStub([(product(1, "149900"),), (out_of_stock,)]),
            store,
            sender,
        )

        price_monitor.scan()
        price_monitor.scan()

        persisted = store.load_price_snapshots("technosapien")

    assert sender.messages == []
    assert str(persisted[0].amount) == "1299"
    assert persisted[0].available is False


def test_woocommerce_price_monitor_alerts_when_out_of_stock_returns(
    tmp_path: Path,
) -> None:
    out_of_stock = WooCommerceProduct.model_validate(
        product_payload(1, price="129900", in_stock=False),
    )
    back_in_stock = WooCommerceProduct.model_validate(
        product_payload(1, price="129900", in_stock=True),
    )
    with DeliveryStore(tmp_path / "state.db") as store:
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
        price_monitor = monitor(
            CatalogStub([(out_of_stock,), (back_in_stock,)]),
            store,
            sender,
            restock_alerts=True,
        )

        price_monitor.scan()
        price_monitor.scan()

        persisted = store.load_price_snapshots("technosapien")

    assert len(sender.messages) == 1
    assert sender.messages[0].entry.restocked is True
    assert persisted[0].available is True
