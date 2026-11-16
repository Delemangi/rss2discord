"""Shared presentation for provider price-change messages."""

from dataclasses import replace

from rss2discord.configuration import FeedConfig
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import EntryData, PriceSnapshot, SourceMetric

from .comparison import price_direction


def build_price_change_message(
    *,
    feed: FeedConfig,
    base_entry: EntryData,
    previous: PriceSnapshot,
    current: PriceSnapshot,
    source_title: str,
) -> WebhookMessage:
    """Build a price-change message from an entry and its price transition."""
    metrics = (
        SourceMetric("Price", current.formatted),
        SourceMetric("Previous", previous.formatted, prior=True),
        *(metric for metric in base_entry.source_metrics if metric.label != "Price"),
    )
    return WebhookMessage(
        feed=feed,
        entry=replace(
            base_entry,
            description="",
            source_metrics=metrics,
            price_direction=price_direction(previous, current),
        ),
        source_title=source_title,
    )
