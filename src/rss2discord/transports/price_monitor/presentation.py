"""Shared presentation for provider price-change messages."""

from dataclasses import replace
from typing import cast

from rss2discord.configuration import FeedConfig
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import EntryData, PriceSnapshot, SourceMetric

from .comparison import price_change_percentage, price_direction


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


def with_price_change_percentage(
    message: WebhookMessage,
    previous: PriceSnapshot,
    current: PriceSnapshot,
) -> WebhookMessage:
    """Attach a signed percentage to a price-change message when comparable.

    An equal amount carries no move to report, so the message is returned
    unchanged instead of gaining a ``+0.0%`` line. A restock at the same price
    therefore stays a stock alert rather than looking like a price change.
    """
    if previous.amount == current.amount:
        return message
    percentage = price_change_percentage(previous, current)
    if percentage is None:
        return message
    return cast(
        WebhookMessage,
        replace(
            message,
            entry=replace(
                message.entry,
                price_change_percentage=percentage,
            ),
        ),
    )


def with_restock(message: WebhookMessage) -> WebhookMessage:
    """Mark a message as a stock alert instead of a price move."""
    return cast(
        WebhookMessage,
        replace(
            message,
            entry=replace(
                message.entry,
                restocked=True,
                price_direction=None,
            ),
        ),
    )
