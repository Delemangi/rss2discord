"""Immutable value objects for the durable price-notification outbox."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from rss2discord.models import EntryData, PriceDirection, SourceMetric
from rss2discord.recovery_models import PriceSnapshot


@dataclass(frozen=True, slots=True)
class PriceNotification:
    """One price change to persist in the outbox before any Discord send."""

    product_id: str
    previous: PriceSnapshot | None
    current: PriceSnapshot
    entry: EntryData
    source_title: str


@dataclass(frozen=True, slots=True)
class PriceNotificationRecord:
    """A pending outbox row loaded back for delivery."""

    notification_id: int
    feed_id: str
    group_id: str
    product_id: str
    title: str
    link: str
    previous_price: str | None
    current_price: str
    currency: str
    entry: EntryData
    source_title: str
    observed_at: int
    attempts: int


class PriceNotificationDeliveryResult(StrEnum):
    """Outcome of delivering one feed's pending notification groups."""

    DRAINED = "drained"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


def entry_to_json(entry: EntryData) -> str:
    """Serialize an entry deterministically so persisted rows are reproducible."""
    payload = {
        "author": entry.author,
        "categories": list(entry.categories),
        "description": entry.description,
        "discussion_url": entry.discussion_url,
        "image_url": entry.image_url,
        "link": entry.link,
        "price_direction": (
            None if entry.price_direction is None else str(entry.price_direction)
        ),
        "source_metrics": [
            {"label": metric.label, "prior": metric.prior, "value": metric.value}
            for metric in entry.source_metrics
        ],
        "timestamp": entry.timestamp,
        "title": entry.title,
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def entry_from_json(value: str) -> EntryData:
    payload = json.loads(value)
    direction = payload.get("price_direction")
    return EntryData(
        title=str(payload["title"]),
        link=str(payload["link"]),
        description=str(payload["description"]),
        author=str(payload["author"]),
        timestamp=(
            None if payload.get("timestamp") is None else str(payload["timestamp"])
        ),
        discussion_url=(
            None
            if payload.get("discussion_url") is None
            else str(payload["discussion_url"])
        ),
        image_url=(
            None if payload.get("image_url") is None else str(payload["image_url"])
        ),
        categories=tuple(str(category) for category in payload.get("categories", ())),
        source_metrics=tuple(
            SourceMetric(
                label=str(metric["label"]),
                value=str(metric["value"]),
                prior=bool(metric["prior"]),
            )
            for metric in payload.get("source_metrics", ())
        ),
        price_direction=None if direction is None else PriceDirection(direction),
    )
