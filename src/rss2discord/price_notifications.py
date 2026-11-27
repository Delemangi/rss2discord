"""Immutable value objects for the durable price-notification outbox."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from rss2discord.defaults import PRICE_HISTORY_WINDOW_DAYS
from rss2discord.models import (
    EntryData,
    PriceDirection,
    PriceHistoryWindow,
    PriceSnapshot,
    SourceMetric,
)


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
    previous_amount: Decimal | None
    current_amount: Decimal
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
        "price_change_percentage": entry.price_change_percentage,
        "discount_percentage": entry.discount_percentage,
        "price_period_low": entry.price_period_low,
        "price_period_high": entry.price_period_high,
        "price_period_days": entry.price_period_days,
        "price_is_period_low": entry.price_is_period_low,
        "price_history": [
            {
                "days": window.days,
                "label": window.label,
                "low": window.low,
                "high": window.high,
                "is_low": window.is_low,
            }
            for window in entry.price_history
        ],
        "restocked": entry.restocked,
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
        price_change_percentage=(
            None
            if payload.get("price_change_percentage") is None
            else str(payload["price_change_percentage"])
        ),
        discount_percentage=(
            None
            if payload.get("discount_percentage") is None
            else str(payload["discount_percentage"])
        ),
        price_period_low=(
            None
            if payload.get("price_period_low") is None
            else str(payload["price_period_low"])
        ),
        price_period_high=(
            None
            if payload.get("price_period_high") is None
            else str(payload["price_period_high"])
        ),
        price_period_days=int(
            payload.get("price_period_days", PRICE_HISTORY_WINDOW_DAYS),
        ),
        price_is_period_low=bool(payload.get("price_is_period_low", False)),
        price_history=_price_history_from_json(payload.get("price_history")),
        restocked=bool(payload.get("restocked", False)),
    )


def _price_history_from_json(value: object) -> tuple[PriceHistoryWindow, ...]:
    if not isinstance(value, list):
        return ()
    windows: list[PriceHistoryWindow] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            days_value = item.get("days")
            windows.append(
                PriceHistoryWindow(
                    days=None if days_value is None else int(days_value),
                    label=str(item["label"]),
                    low=str(item["low"]),
                    high=str(item["high"]),
                    is_low=bool(item.get("is_low", False)),
                ),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(windows)
