from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import PriceDirection
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.price_monitor import PriceAlertDelivery
from rss2discord.transports.technomarket_models import TechnomarketProduct
from rss2discord.transports.technomarket_price_monitor import (
    TechnomarketPriceMonitor,
    TechnomarketPriceMonitorDependencies,
)
from tests.setec_price_monitor_helpers import RecordingSender


class CatalogStub:
    def __init__(self, batches: list[tuple[TechnomarketProduct, ...]]) -> None:
        self._batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[TechnomarketProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._batches.pop(0)


def product(
    regular: str,
    smart: str | None = None,
    product_id: str = "12345",
) -> TechnomarketProduct:
    return TechnomarketProduct(
        product_id=product_id,
        name="Alpha Laptop",
        url=f"https://tehnomarket.com.mk/product/alpha-{product_id}/",
        image_url=None,
        manufacturer="AlphaTech",
        categories=("Лаптопи",),
        regular_price=Decimal(regular),
        smart_price=None if smart is None else Decimal(smart),
    )


def dependencies(
    catalog: CatalogStub, sender: RecordingSender,
) -> TechnomarketPriceMonitorDependencies:
    return TechnomarketPriceMonitorDependencies(
        catalog=catalog,
        snapshots=None,  # type: ignore[arg-type]
        sender=sender,
        fetch_retry_policy=FetchRetryPolicy(
            sleep=lambda _: True, on_retry=lambda *_: None,
        ),
        sqlite_retry_policy=SQLiteRetryPolicy(
            sleep=lambda _: True, on_retry=lambda *_: None,
        ),
        delivery=PriceAlertDelivery(
            sleep=lambda _: True,
            delay_between_posts=0,
            is_shutdown_requested=lambda: False,
        ),
    )


def test_technomarket_price_monitor_baselines_and_alerts_effective_smart_changes(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    feed = FeedConfig(
        id="technomarket",
        url="https://tehnomarket.com.mk/category/42/laptops",
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="technomarket",
    )
    catalog = CatalogStub([(product("59999", "56999"),), (product("57999", "54999"),)])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = TechnomarketPriceMonitor(
            feed,
            replace(dependencies(catalog, sender), snapshots=store),
        )
        monitor.scan()
        monitor.scan()

        assert len(sender.messages) == 1
        assert sender.messages[0].entry.price_direction is PriceDirection.DECREASE
        assert str(store.load_price_snapshots("technomarket")[0].amount) == "54999"


def test_technomarket_regular_only_change_is_silent_when_smart_price_is_unchanged(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([])
    feed = FeedConfig(
        id="technomarket",
        url="https://tehnomarket.com.mk/category/42/laptops",
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="technomarket",
    )
    catalog = CatalogStub([(product("59999", "56999"),), (product("58999", "56999"),)])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = TechnomarketPriceMonitor(
            feed,
            replace(dependencies(catalog, sender), snapshots=store),
        )
        monitor.scan()
        monitor.scan()

        assert sender.messages == []
        assert str(store.load_price_snapshots("technomarket")[0].amount) == "56999"
