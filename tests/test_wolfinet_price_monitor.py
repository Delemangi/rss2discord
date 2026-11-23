"""Price monitoring tests for the Wolfinet shop catalog."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import PriceDirection, PriceSnapshot, SourceMetric
from rss2discord.providers.wolfinet import prices as wolfinet_prices
from rss2discord.providers.wolfinet.models import WolfinetProduct
from rss2discord.providers.wolfinet.prices import (
    WolfinetCatalog,
    WolfinetPriceMonitor,
)
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceMonitorDependencies,
)
from tests.setec_price_monitor_helpers import RecordingSender

WolfinetPriceMonitorDependencies = PriceMonitorDependencies[WolfinetCatalog]


class CatalogStub:
    def __init__(self, batches: list[tuple[WolfinetProduct, ...]]) -> None:
        self._batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[WolfinetProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._batches.pop(0)


def feed() -> FeedConfig:
    return FeedConfig(
        id="wolfinet",
        name="Wolfinet Shop",
        url="https://wolfinet.com.mk/en/shop",
        webhook="https://discord.test/webhook",
        strategy="wolfinet",
    )


def product(
    product_id: str = "4101",
    *,
    amount: str = "2364.41",
    formatted: str | None = None,
    name: str = "Ubiquiti UniFi 6 Lite",
) -> WolfinetProduct:
    return WolfinetProduct(
        product_id=product_id,
        name=name,
        url=f"https://wolfinet.com.mk/en/shop/product-{product_id}",
        image_url=f"https://wolfinet.com.mk/web/image/product.template/{product_id}/image",
        description="",
        amount=Decimal(amount),
        formatted_price=formatted or f"{amount} ден",
        currency="MKD",
        observed_at=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
    )


def monitor(
    catalog: CatalogStub,
    store: DeliveryStore,
    sender: RecordingSender,
) -> WolfinetPriceMonitor:
    return WolfinetPriceMonitor(
        feed(),
        WolfinetPriceMonitorDependencies(
            catalog=catalog,
            snapshots=store,
            sender=sender,
            fetch_retry_policy=FetchRetryPolicy(
                sleep=lambda _seconds: True,
                on_retry=lambda _error, _delay: None,
            ),
            sqlite_retry_policy=SQLiteRetryPolicy(
                sleep=lambda _seconds: True,
                on_retry=lambda _error, _delay: None,
            ),
            delivery=PriceAlertDelivery(
                sleep=lambda _seconds: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
        ),
    )


def test_wolfinet_price_monitor_baselines_then_alerts_on_a_price_drop(
    tmp_path: Path,
) -> None:
    sender = RecordingSender(
        [DiscordDeliveryResult.DELIVERED],
    )
    catalog = CatalogStub(
        [
            (product(amount="2364.41", formatted="2,364.41 ден"),),
            (product(amount="1999.00", formatted="1,999.00 ден"),),
        ],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(catalog, store, sender)
        price_monitor.scan()
        assert sender.messages == []
        assert store.load_price_snapshots("wolfinet") == (
            PriceSnapshot(
                "wolfinet",
                "4101",
                Decimal("2364.41"),
                "2,364.41 ден",
                "MKD",
            ),
        )

        price_monitor.scan()

        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert message.source_title == "Wolfinet Shop"
        assert message.entry.title == "Ubiquiti UniFi 6 Lite"
        assert message.entry.link == ("https://wolfinet.com.mk/en/shop/product-4101")
        assert message.entry.image_url == (
            "https://wolfinet.com.mk/web/image/product.template/4101/image"
        )
        assert message.entry.categories == ()
        assert message.entry.price_direction is PriceDirection.DECREASE
        assert message.entry.source_metrics == (
            SourceMetric("Price", "1,999.00 ден"),
            SourceMetric("Previous", "2,364.41 ден", prior=True),
        )
        assert store.load_price_snapshots("wolfinet") == (
            PriceSnapshot(
                "wolfinet",
                "4101",
                Decimal("1999.00"),
                "1,999.00 ден",
                "MKD",
            ),
        )


def test_wolfinet_price_monitor_skips_non_positive_prices(tmp_path: Path) -> None:
    sender = RecordingSender([])
    catalog = CatalogStub(
        [
            (product("zero", amount="0.00"), product("negative", amount="-1.00")),
        ],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor(catalog, store, sender).scan()

        assert sender.messages == []
        assert store.load_price_snapshots("wolfinet") == ()


def test_wolfinet_price_monitor_keeps_one_snapshot_per_duplicate_id(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([])
    catalog = CatalogStub(
        [
            (product(amount="100.00"), product(amount="200.00")),
            (product(amount="100.00"),),
        ],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(catalog, store, sender)
        price_monitor.scan()

        assert store.load_price_snapshots("wolfinet") == (
            PriceSnapshot("wolfinet", "4101", Decimal("100.00"), "100.00 ден", "MKD"),
        )

        price_monitor.scan()

        assert sender.messages == []


def test_wolfinet_price_monitor_rejects_a_catalog_above_the_snapshot_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wolfinet_prices, "MAX_WOLFINET_RETAINED_SNAPSHOTS", 2)
    sender = RecordingSender([])
    catalog = CatalogStub(
        [
            (
                product("1"),
                product("2"),
                product("3"),
            ),
        ],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(catalog, store, sender)
        with pytest.raises(FeedFetchError) as error:
            price_monitor.scan()

    assert error.value.cause_type == "SnapshotLimitExceeded"
    assert sender.messages == []


def test_wolfinet_price_monitor_keeps_the_first_duplicate_identity() -> None:
    first = replace(product(), name="First")
    second = replace(product(), name="Second")

    assert WolfinetPriceMonitor._priced_products((first, second)) == (first,)
