"""Delivery-safe actual-price monitoring for one Neptun category."""

from collections.abc import Callable
from dataclasses import replace
from typing import Final, Protocol

from rss2discord.discord.message import WebhookMessage
from rss2discord.models import PriceSnapshot, SourceMetric
from rss2discord.providers.catalog_price_monitor import CatalogPriceMonitor
from rss2discord.providers.neptun.http import NEPTUN_LABEL
from rss2discord.providers.neptun.models import NeptunProduct
from rss2discord.providers.neptun.strategy import NeptunStrategy, format_neptun_mkd
from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.price_monitor import price_direction

MAX_NEPTUN_RETAINED_SNAPSHOTS: Final = 10_000


class NeptunCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[NeptunProduct, ...]: ...


class NeptunPriceMonitor(CatalogPriceMonitor[NeptunProduct]):
    """Compare positive actual prices and persist changes after Discord delivery."""

    provider = "neptun"
    label = NEPTUN_LABEL

    @property
    def snapshot_limit(self) -> int:
        return MAX_NEPTUN_RETAINED_SNAPSHOTS

    def _snapshot(self, product: NeptunProduct) -> PriceSnapshot | None:
        if product.actual_price <= 0:
            return None
        return PriceSnapshot(
            self._feed.id,
            str(product.id),
            product.actual_price,
            format_neptun_mkd(product.actual_price),
            "MKD",
            available=product.available_online and product.available_webshop,
        )

    def _message_for(
        self,
        product: NeptunProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        base_entry = NeptunStrategy().get_entry_data(product)
        metrics = [
            SourceMetric("Price", current.formatted),
            SourceMetric("Previous", previous.formatted, prior=True),
        ]
        if product.regular_price != product.actual_price:
            metrics.append(
                SourceMetric(
                    "Original",
                    format_neptun_mkd(product.regular_price),
                ),
            )
        metrics.extend(
            (
                SourceMetric("Manufacturer", product.manufacturer.name),
                SourceMetric("Code", product.code_number),
                SourceMetric(
                    "Online",
                    "Available"
                    if product.available_online and product.available_webshop
                    else "Unavailable",
                ),
            ),
        )
        return WebhookMessage(
            feed=self._feed,
            entry=replace(
                base_entry,
                description="",
                source_metrics=tuple(metrics),
                price_direction=price_direction(previous, current),
            ),
            source_title=self._feed.name or NEPTUN_LABEL,
        )
