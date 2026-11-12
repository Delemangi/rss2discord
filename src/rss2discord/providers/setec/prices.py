"""Two-phase calculated-price comparison and Discord delivery for one Setec feed."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import (
    DiscordSender,
    WebhookMessage,
)
from rss2discord.models import EntryData, SourceMetric
from rss2discord.providers.setec.catalog_bounds import SETEC_LABEL
from rss2discord.providers.setec.models import SetecPriceEntry, SetecProduct
from rss2discord.providers.setec.strategy import (
    SETEC_PRODUCT_BASE_URL,
    format_setec_mkd,
)
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.price_monitor import (
    NO_SNAPSHOT_LIMIT,
    PriceAlertDelivery,
    PriceObservation,
    PriceRecoveryStore,
    price_direction,
    run_price_scan,
)

logger = logging.getLogger(__name__)


class SetecCatalog(Protocol):
    """Retrieve Setec prices, then display data for selected products only."""

    def fetch_price_index(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecPriceEntry, ...]: ...

    def fetch_products_by_ids(
        self,
        url: str,
        product_ids: Sequence[str],
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecProduct, ...]: ...


@dataclass(frozen=True, slots=True)
class SetecPriceMonitorDependencies:
    """Typed collaborators used by one Setec price-monitor scan."""

    catalog: SetecCatalog
    snapshots: PriceRecoveryStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _SetecObservation:
    index: SetecPriceEntry
    detail: SetecProduct | None = None


class SetecPriceMonitor:
    """Compare one price index against persisted snapshots and alert on changes."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: SetecPriceMonitorDependencies,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies

    def scan(self) -> None:
        self._scan()

    def _scan(self) -> None:
        """Compare prices, persist silent updates, then alert on changed products."""
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        price_entries = self._dependencies.catalog.fetch_price_index(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        observations: list[PriceObservation[_SetecObservation]] = []
        for entry in price_entries:
            current = self._snapshot(entry)
            if current is not None:
                observations.append(
                    PriceObservation(_SetecObservation(entry), current),
                )
        ambiguous_count = sum(len(entry.variants) > 1 for entry in price_entries)
        if ambiguous_count:
            logger.warning(
                "Deferred %d ambiguous multi-variant prices for feed %s",
                ambiguous_count,
                self._feed.id,
            )
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider="setec",
            label=SETEC_LABEL,
            observations=observations,
            message_for=self._message_for,
            snapshot_limit=NO_SNAPSHOT_LIMIT,
            confirm=self._confirm_observations,
            confirmation_limit=2000,
        )

    def _confirm_observations(
        self,
        observations: Sequence[PriceObservation[_SetecObservation]],
    ) -> list[PriceObservation[_SetecObservation]]:
        products = self._dependencies.catalog.fetch_products_by_ids(
            self._feed.url,
            [observation.product.index.id for observation in observations],
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        products_by_id = {product.id: product for product in products}
        confirmed: list[PriceObservation[_SetecObservation]] = []
        for observation in observations:
            index = observation.product.index
            product = products_by_id.get(index.id)
            if product is None or len(product.variants) != 1:
                continue
            variant = product.variants[0]
            index_variant_id = index.variants[0].id if index.variants else None
            if (
                not index_variant_id
                or not variant.id
                or variant.id != index_variant_id
                or variant.calculated_price.currency_code.upper()
                != observation.snapshot.currency
                or variant.calculated_price.calculated_amount
                != observation.snapshot.amount
            ):
                continue
            confirmed.append(
                PriceObservation(
                    _SetecObservation(index, product),
                    observation.snapshot,
                ),
            )
        deferred_count = len(observations) - len(confirmed)
        if deferred_count:
            logger.warning(
                "Deferred %d unconfirmed price changes for feed %s; "
                "product details must agree with indexed price and variant",
                deferred_count,
                self._feed.id,
            )
        return confirmed

    def _snapshot(self, entry: SetecPriceEntry) -> PriceSnapshot | None:
        calculated_amount = entry.calculated_amount
        if calculated_amount is None:
            return None
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=entry.id,
            amount=calculated_amount,
            formatted=format_setec_mkd(calculated_amount),
            currency="MKD",
        )

    def _message_for(
        self,
        observation: _SetecObservation,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        product = observation.detail
        if product is None:
            raise ValueError("confirmed Setec observation requires product details")
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=product.title,
                link=f"{SETEC_PRODUCT_BASE_URL}{product.handle}",
                description="",
                author="",
                timestamp=None,
                image_url=product.thumbnail,
                categories=tuple(category.name for category in product.categories),
                source_metrics=self._metrics_for(product, previous, current),
                price_direction=price_direction(previous, current),
            ),
            source_title=self._feed.name or SETEC_LABEL,
        )

    @staticmethod
    def _metrics_for(
        product: SetecProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> tuple[SourceMetric, ...]:
        metrics = [
            SourceMetric(label="Price", value=current.formatted),
            SourceMetric(label="Previous", value=previous.formatted, prior=True),
        ]
        if not product.variants:
            return tuple(metrics)
        calculated_price = product.variants[0].calculated_price
        if calculated_price.calculated_amount != current.amount:
            # The display fetch saw a different price than the one being reported,
            # so its original amount does not belong beside this alert's price.
            return tuple(metrics)
        if calculated_price.original_amount != calculated_price.calculated_amount:
            metrics.append(
                SourceMetric(
                    label="Original",
                    value=format_setec_mkd(calculated_price.original_amount),
                ),
            )
        return tuple(metrics)
