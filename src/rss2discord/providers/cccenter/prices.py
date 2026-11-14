"""Opt-in CCCenter catalog price monitoring."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData, SourceMetric
from rss2discord.providers.cccenter.bounds import (
    CCCENTER_LABEL,
    MAX_CCCENTER_RETAINED_SNAPSHOTS,
)
from rss2discord.providers.cccenter.catalog import MAX_CCCENTER_DETAIL_PRODUCTS
from rss2discord.providers.cccenter.models import CCCenterListing, CCCenterProduct
from rss2discord.providers.cccenter.strategy import format_cccenter_mkd
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceObservation,
    PriceRecoveryStore,
    price_direction,
    run_price_scan,
)


class CCCenterCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[CCCenterProduct, ...]: ...

    def fetch_product_details(
        self,
        listings: Sequence[CCCenterListing | CCCenterProduct],
        *,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[CCCenterProduct, ...]: ...


class CCCenterSnapshotStore(PriceRecoveryStore, Protocol):
    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class CCCenterPriceMonitorDependencies:
    catalog: CCCenterCatalog
    snapshots: CCCenterSnapshotStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


class CCCenterPriceMonitor:
    def __init__(
        self,
        feed: FeedConfig,
        dependencies: CCCenterPriceMonitorDependencies,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies
        self._observation_order: dict[str, int] = {}

    def scan(self) -> None:
        self._scan()

    def _scan(self) -> None:
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        products = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        available = tuple(
            product
            for product in products
            if (
                product.price_status == "scalar"
                and product.current_price is not None
                and product.current_price > 0
            )
        )
        observations = tuple(
            PriceObservation(product, self._snapshot(product)) for product in available
        )
        self._observation_order = {
            observation.snapshot.product_id: index
            for index, observation in enumerate(observations)
        }
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider="cccenter",
            label=CCCENTER_LABEL,
            observations=observations,
            message_for=self._message,
            snapshot_limit=MAX_CCCENTER_RETAINED_SNAPSHOTS,
            confirm=self._confirm_observations,
            confirmation_limit=100,
        )

    def _confirm_observations(
        self,
        observations: Sequence[PriceObservation[CCCenterProduct]],
    ) -> list[PriceObservation[CCCenterProduct]]:
        confirmed: list[PriceObservation[CCCenterProduct]] = []
        for start in range(0, len(observations), MAX_CCCENTER_DETAIL_PRODUCTS):
            if self._dependencies.delivery.is_shutdown_requested():
                raise FeedFetchInterruptedError
            chunk = observations[start : start + MAX_CCCENTER_DETAIL_PRODUCTS]
            details = self._dependencies.catalog.fetch_product_details(
                tuple(observation.product for observation in chunk),
                is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
            )
            selected_ids = {observation.snapshot.product_id for observation in chunk}
            by_id: dict[str, CCCenterProduct] = {}
            for product in details:
                if (
                    product.product_id not in selected_ids
                    or product.url != product.product_id
                    or product.product_id in by_id
                ):
                    raise FeedFetchError(CCCENTER_LABEL, "InvalidProductIdentity")
                by_id[product.product_id] = product
            confirmed.extend(
                PriceObservation(
                    by_id[observation.snapshot.product_id],
                    observation.snapshot,
                )
                for observation in chunk
                if observation.snapshot.product_id in by_id
                and by_id[observation.snapshot.product_id].price_status == "scalar"
                and observation.snapshot.currency == "MKD"
                and by_id[observation.snapshot.product_id].current_price
                == observation.snapshot.amount
            )
            if len(confirmed) >= 100:
                break
        confirmed.sort(
            key=lambda observation: self._observation_order.get(
                observation.snapshot.product_id,
                len(self._observation_order),
            ),
        )
        return confirmed

    def _snapshot(self, product: CCCenterProduct) -> PriceSnapshot:
        if product.current_price is None:
            raise FeedFetchError(CCCENTER_LABEL, "UnavailablePrice")
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=product.product_id,
            amount=product.current_price,
            formatted=format_cccenter_mkd(product.current_price),
            currency="MKD",
        )

    def _message(
        self,
        product: CCCenterProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=product.name,
                link=product.url,
                description="",
                author="",
                timestamp=None,
                image_url=product.image_url,
                categories=product.categories,
                source_metrics=(
                    SourceMetric("Price", format_cccenter_mkd(product.current_price))
                    if product.current_price is not None
                    else SourceMetric("Price", "Unavailable"),
                    SourceMetric("Previous", previous.formatted, prior=True),
                    *(
                        (
                            SourceMetric(
                                "Original",
                                format_cccenter_mkd(product.original_price),
                            ),
                        )
                        if product.original_price is not None
                        and product.original_price != product.current_price
                        else ()
                    ),
                    SourceMetric(
                        "Stock",
                        "In stock" if product.is_in_stock else "Out of stock",
                    ),
                    *((SourceMetric("SKU", product.sku),) if product.sku else ()),
                ),
                price_direction=price_direction(previous, current),
            ),
            source_title=self._feed.name or CCCENTER_LABEL,
        )
