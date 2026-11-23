"""OpenRouter significant price-drop monitor."""

from dataclasses import replace
from decimal import Decimal

from rss2discord.discord.message import WebhookMessage
from rss2discord.models import PriceSnapshot, SourceMetric
from rss2discord.providers.catalog_price_monitor import CatalogPriceMonitor
from rss2discord.providers.openrouter_models.bounds import (
    MAX_OPENROUTER_PRICE_SNAPSHOTS,
)
from rss2discord.providers.openrouter_models.models import OpenRouterModel
from rss2discord.providers.openrouter_models.strategy import (
    OPENROUTER_SOURCE_TITLE,
    OpenRouterModelsStrategy,
    format_price,
    is_model_entry,
)
from rss2discord.transports.price_monitor import price_direction

_PRICE_UNIT = Decimal(1_000_000)


class OpenRouterPriceMonitor(CatalogPriceMonitor[OpenRouterModel]):
    """Alert on meaningful input-price drops without dynamic-pricing churn."""

    provider = "openrouter_prices"
    label = "OpenRouter Prices"
    positive_prices_only = True
    empty_catalog_cause = "EmptyCatalog"

    @property
    def snapshot_limit(self) -> int:
        return MAX_OPENROUTER_PRICE_SNAPSHOTS

    def _snapshot(self, model: OpenRouterModel) -> PriceSnapshot | None:
        if not is_model_entry(model):
            return None
        price = model.pricing.prompt
        if price is None or price <= 0:
            return None
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=model.id,
            amount=price * _PRICE_UNIT,
            formatted=format_price(price),
            currency="USD",
            available=None,
        )

    def _message_for(
        self,
        model: OpenRouterModel,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        base = OpenRouterModelsStrategy().get_entry_data(model)
        metrics = (
            SourceMetric("Price", current.formatted),
            SourceMetric("Previous", previous.formatted, prior=True),
            *(metric for metric in base.source_metrics if metric.label != "Price"),
        )
        return WebhookMessage(
            feed=self._feed,
            entry=replace(
                base,
                source_metrics=metrics,
                price_direction=price_direction(previous, current),
            ),
            source_title=self._feed.name or OPENROUTER_SOURCE_TITLE,
        )
