"""Persist and report feed health and same-origin cooldowns."""

import logging
import re
import sqlite3
from typing import Literal
from urllib.parse import urlsplit

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.recovery_models import HealthUpdate

logger = logging.getLogger("rss2discord.price_runtime")


def _origin(url: str) -> tuple[str, str | None, int | None] | None:
    try:
        parts = urlsplit(url)
        origin = (
            parts.scheme,
            parts.hostname,
            parts.port or (443 if parts.scheme == "https" else 80),
        )
    except ValueError:
        return None
    return origin


def feed_is_blocked(
    feed: FeedConfig,
    feeds: tuple[FeedConfig, ...],
    store: DeliveryStore,
    now: int,
) -> bool:
    """Persisted feed cooldowns also cover configured peers on the same origin."""
    return any(
        (store.get_blocked_until(feed_id) or 0) > now
        for feed_id in cooldown_peer_ids(feed, feeds)
    )


def cooldown_peer_ids(
    feed: FeedConfig,
    feeds: tuple[FeedConfig, ...],
) -> tuple[str, ...]:
    origin = _origin(feed.url)
    # Bounded by the configured feeds; only IDs cross into worker dependencies.
    return tuple(
        dict.fromkeys(
            (
                feed.id,
                *(
                    peer.id
                    for peer in feeds
                    if origin is not None and _origin(peer.url) == origin
                ),
            ),
        ),
    )


def record_runtime_health(store: DeliveryStore, update: HealthUpdate) -> None:
    """Only transitions/reminders log; diagnostic payloads contain no URLs."""
    try:
        notice = store.record_health(update, reminder_seconds=21600)
    except sqlite3.Error as error:
        logger.log(
            logging.ERROR,
            "Health persistence failed for feed %s (%s)",
            update.feed_id,
            type(error).__name__,
        )
        return
    if notice.should_log:
        level = logging.ERROR if update.state in {"failed", "blocked"} else logging.INFO
        logger.log(
            level,
            "Feed %s %s health=%s cause=%s recovered=%s",
            update.feed_id,
            update.job_kind,
            update.state,
            update.cause,
            notice.recovered,
        )


def record_fetch_failure(
    store: DeliveryStore,
    feed_id: str,
    job_kind: Literal["ordinary", "price"],
    error: Exception,
    attempted_at: int,
    *,
    duration_ms: int,
) -> None:
    cause = safe_error_cause(error)
    blocked = isinstance(error, FeedFetchError) and (
        error.cause_type in {"BotChallenge", "AccessChallenge"}
        or error.status_code == 403
    )
    record_runtime_health(
        store,
        HealthUpdate(
            feed_id=feed_id,
            job_kind=job_kind,
            state="blocked" if blocked else "failed",
            cause=cause,
            attempted_at=attempted_at,
            success=False,
            nonempty=False,
            item_count=0,
            duration_ms=duration_ms,
            scheduler_lag_ms=0,
        ),
    )


def safe_error_cause(error: Exception) -> str:
    cause = (
        error.cause_type if isinstance(error, FeedFetchError) else type(error).__name__
    )
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", cause):
        return "FetchError"
    if (
        isinstance(error, FeedFetchError)
        and isinstance(error.status_code, int)
        and 100 <= error.status_code <= 599
    ):
        return f"{cause} HTTP {error.status_code}"
    return cause
