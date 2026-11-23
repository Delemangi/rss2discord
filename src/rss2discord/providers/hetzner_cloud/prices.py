"""Hetzner Cloud restock-aware price monitor."""

from datetime import UTC, datetime
from decimal import Decimal

from rss2discord.discord.message import WebhookMessage
from rss2discord.models import EntryData, PriceSnapshot, SourceMetric
from rss2discord.providers.catalog_price_monitor import CatalogPriceMonitor
from rss2discord.providers.hetzner_cloud.bounds import (
    HETZNER_CLOUD_CONSOLE_URL,
    HETZNER_CLOUD_CURRENCY,
    HETZNER_CLOUD_LABEL,
    HETZNER_CLOUD_LOCATION_LABELS,
    MAX_HETZNER_CLOUD_SNAPSHOTS,
)
from rss2discord.providers.hetzner_cloud.models import HetznerCloudOffer
from rss2discord.transports.price_monitor import build_price_change_message


class HetznerCloudPriceMonitor(CatalogPriceMonitor[HetznerCloudOffer]):
    """Alert when a server type becomes available in a tracked location."""

    provider = "hetzner_cloud"
    label = HETZNER_CLOUD_LABEL
    positive_prices_only = True
    empty_catalog_cause = "EmptyCatalog"

    @property
    def snapshot_limit(self) -> int:
        return MAX_HETZNER_CLOUD_SNAPSHOTS

    def _snapshot(self, offer: HetznerCloudOffer) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=f"{offer.server_type.name}:{offer.location.name}",
            amount=offer.price,
            formatted=format_hetzner_cloud_price(offer.price),
            currency=HETZNER_CLOUD_CURRENCY,
            available=offer.location.available,
        )

    def _message_for(
        self,
        offer: HetznerCloudOffer,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        server = offer.server_type
        location = offer.location
        metrics = [
            SourceMetric("Location", _location_label(location.name)),
            SourceMetric("Type", server.name),
            SourceMetric("CPU", f"{server.cores} vCPU"),
            SourceMetric("RAM", f"{_format_memory(server.memory)} GB"),
            SourceMetric("Disk", f"{server.disk} GB"),
            SourceMetric("Architecture", server.architecture.upper()),
        ]
        deprecation = location.deprecation or server.deprecation
        if deprecation is not None and deprecation.unavailable_after:
            metrics.append(
                SourceMetric("Deprecating", deprecation.unavailable_after),
            )
        return build_price_change_message(
            feed=self._feed,
            base_entry=EntryData(
                title=f"{server.name} · {_location_label(location.name)}",
                link=HETZNER_CLOUD_CONSOLE_URL,
                description="",
                author=HETZNER_CLOUD_LABEL,
                timestamp=datetime.now(UTC).isoformat(),
                categories=("cloud", location.name),
                source_metrics=tuple(metrics),
            ),
            previous=previous,
            current=current,
            source_title=self._feed.name or HETZNER_CLOUD_LABEL,
        )


def format_hetzner_cloud_price(amount: Decimal) -> str:
    return f"€{amount.quantize(Decimal('0.01'))}/mo"


def _format_memory(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _location_label(name: str) -> str:
    city = HETZNER_CLOUD_LOCATION_LABELS.get(name)
    return f"{city} ({name})" if city is not None else name
