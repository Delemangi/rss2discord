"""Price monitoring tests for the PROXNet category catalog."""

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
from rss2discord.providers.proxnet import prices as proxnet_prices
from rss2discord.providers.proxnet.models import ProxnetProduct
from rss2discord.providers.proxnet.prices import (
    ProxnetCatalog,
    ProxnetPriceMonitor,
)
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceMonitorDependencies,
)
from tests.setec_price_monitor_helpers import RecordingSender

ProxnetPriceMonitorDependencies = PriceMonitorDependencies[ProxnetCatalog]


class CatalogStub:
    def __init__(self, batches: list[tuple[ProxnetProduct, ...]]) -> None:
        self._batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[ProxnetProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._batches.pop(0)


def feed() -> FeedConfig:
    return FeedConfig(
        id="proxnet",
        name="PROXNet Networking",
        url="https://www.proxnet.mk/mk/17-mrezhna-oprema",
        webhook="https://discord.test/webhook",
        strategy="proxnet",
    )


def product(
    product_id: str = "1152",
    *,
    amount: str = "100.00",
    formatted: str | None = None,
    name: str = "Heat Shrink Sleeves 40 mm",
) -> ProxnetProduct:
    return ProxnetProduct(
        product_id=product_id,
        name=name,
        url=f"https://www.proxnet.mk/mk/{product_id}-product.html",
        image_url=f"https://www.proxnet.mk/{product_id}.jpg",
        description="",
        amount=Decimal(amount),
        formatted_price=formatted or f"{amount} ден",
        currency="MKD",
        observed_at=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
        category="Мрежна опрема",
    )


def monitor(
    catalog: CatalogStub,
    store: DeliveryStore,
    sender: RecordingSender,
) -> ProxnetPriceMonitor:
    return ProxnetPriceMonitor(
        feed(),
        ProxnetPriceMonitorDependencies(
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


def test_proxnet_price_monitor_baselines_then_alerts_on_a_price_drop(
    tmp_path: Path,
) -> None:
    sender = RecordingSender(
        [DiscordDeliveryResult.DELIVERED],
    )
    catalog = CatalogStub(
        [
            (product(amount="9.00", formatted="9.00 ден"),),
            (product(amount="8.00", formatted="8.00 ден"),),
        ],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(catalog, store, sender)
        price_monitor.scan()
        assert sender.messages == []
        assert store.load_price_snapshots("proxnet") == (
            PriceSnapshot(
                "proxnet",
                "1152",
                Decimal("9.00"),
                "9.00 ден",
                "MKD",
            ),
        )

        price_monitor.scan()

        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert message.source_title == "PROXNet Networking"
        assert message.entry.title == "Heat Shrink Sleeves 40 mm"
        assert message.entry.link == "https://www.proxnet.mk/mk/1152-product.html"
        assert message.entry.image_url == "https://www.proxnet.mk/1152.jpg"
        assert message.entry.categories == ("Мрежна опрема",)
        assert message.entry.price_direction is PriceDirection.DECREASE
        assert message.entry.source_metrics == (
            SourceMetric("Price", "8.00 ден"),
            SourceMetric("Previous", "9.00 ден", prior=True),
        )
        assert store.load_price_snapshots("proxnet") == (
            PriceSnapshot(
                "proxnet",
                "1152",
                Decimal("8.00"),
                "8.00 ден",
                "MKD",
            ),
        )


def test_proxnet_price_monitor_skips_non_positive_prices(tmp_path: Path) -> None:
    sender = RecordingSender([])
    catalog = CatalogStub(
        [
            (product("zero", amount="0.00"), product("negative", amount="-1.00")),
        ],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor(catalog, store, sender).scan()

        assert sender.messages == []
        assert store.load_price_snapshots("proxnet") == ()


def test_proxnet_price_monitor_keeps_one_snapshot_per_duplicate_id(
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

        assert store.load_price_snapshots("proxnet") == (
            PriceSnapshot("proxnet", "1152", Decimal("100.00"), "100.00 ден", "MKD"),
        )

        price_monitor.scan()

        assert sender.messages == []


def test_proxnet_price_monitor_rejects_a_catalog_above_the_snapshot_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proxnet_prices, "MAX_PROXNET_RETAINED_SNAPSHOTS", 2)
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

    with (
        DeliveryStore(tmp_path / "state.db") as store,
        pytest.raises(FeedFetchError) as error,
    ):
        monitor(catalog, store, sender).scan()

    assert error.value.cause_type == "SnapshotLimitExceeded"
    assert sender.messages == []


def test_proxnet_price_monitor_keeps_the_first_duplicate_category_identity() -> None:
    first = replace(product(), category="Мрежна опрема")
    second = replace(product(), category="Друга категорија")

    assert ProxnetPriceMonitor._priced_products((first, second)) == (first,)
