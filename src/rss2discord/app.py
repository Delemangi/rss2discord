import logging
import random
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from .adapters import AdapterError, HackerNewsAdapter, RedditAdapter, SourceAdapter
from .configuration import AppConfig, FeedConfig
from .delivery_store import DeliveryStore
from .discord.client import DiscordSender, WebhookMessage
from .models import EntryData, EntryId
from .transports import FeedFetchError, RSSStrategy, ScraperStrategy, XenForoStrategy

logger = logging.getLogger(__name__)
PERSISTENCE_RETRY_DELAY_SECONDS: Final = 5.0
FEED_FETCH_MAX_ATTEMPTS: Final = 3
FEED_FETCH_BASE_DELAY_SECONDS: Final = 2.0
FEED_FETCH_MAX_DELAY_SECONDS: Final = 300.0
FEED_FETCH_MAX_BACKOFF_SECONDS: Final = 30.0
MAX_HACKER_NEWS_ENRICHMENTS_PER_FEED: Final = 5
SQLITE_TRANSIENT_ERROR_CODES: Final = frozenset(
    {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED},
)


class FeedFetchInterruptedError(Exception):
    pass


def _log_feed_fetch_error(feed_id: str, error: FeedFetchError) -> None:
    logger.error("Error processing feed %s: %s", feed_id, error)


class RSSToDiscord:
    def __init__(
        self,
        config: AppConfig,
        store: DeliveryStore,
        sender: DiscordSender,
    ) -> None:
        self._config = config
        self._store = store
        self._sender = sender
        self._strategies: dict[str, ScraperStrategy] = {
            "rss": RSSStrategy(),
            "xenforo": XenForoStrategy(),
        }
        self._adapters: dict[str, SourceAdapter] = {
            "hackernews": HackerNewsAdapter(),
            "reddit": RedditAdapter(),
        }
        self._shutdown_requested = False

    def request_shutdown(self) -> None:
        logger.info("Shutdown requested")
        self._shutdown_requested = True

    def process_feed(self, feed: FeedConfig) -> None:
        logger.info("Processing feed %s with strategy %s", feed.id, feed.strategy)
        strategy = self._strategies[feed.strategy]
        entries, fetched_source_title = self._fetch_entries(feed, strategy)
        source_title = feed.name or fetched_source_title
        seen_entry_ids: set[EntryId] = set()
        adapter = self._adapters[feed.adapter] if feed.adapter is not None else None
        hacker_news_enrichments_remaining = (
            MAX_HACKER_NEWS_ENRICHMENTS_PER_FEED
            if feed.adapter == "hackernews"
            else None
        )
        enrichment_limit_logged = False

        for entry in entries:
            if self._shutdown_requested:
                return

            entry_id = strategy.get_entry_id(entry)
            if entry_id is None:
                logger.warning("Skipping entry without a stable ID in feed %s", feed.id)
                continue
            is_seen = entry_id in seen_entry_ids
            seen_entry_ids.add(entry_id)

            if is_seen or self._store.has_delivered(feed.id, entry_id):
                continue

            entry_data = strategy.get_entry_data(entry)
            if adapter is not None and hacker_news_enrichments_remaining != 0:
                try:
                    entry_data = adapter.adapt(entry, entry_data)
                except AdapterError as error:
                    logger.warning(
                        "Adapter %s failed for feed %s (%s); using baseline data",
                        feed.adapter,
                        feed.id,
                        type(error).__name__,
                    )
                if hacker_news_enrichments_remaining is not None:
                    hacker_news_enrichments_remaining -= 1
            elif adapter is not None and not enrichment_limit_logged:
                logger.warning(
                    "Hacker News enrichment limit reached for feed %s; "
                    "using baseline data",
                    feed.id,
                )
                enrichment_limit_logged = True
            if self._is_too_old(entry_data, feed.id):
                continue

            message = WebhookMessage(
                feed=feed,
                entry=entry_data,
                source_title=source_title,
            )
            if not self._sender.send(message, self._interruptible_sleep):
                continue

            if not self._persist_delivery(feed.id, entry_id):
                return
            if not self._interruptible_sleep(self._config.delay_between_posts):
                return

    def run(self) -> None:
        if not self._config.feeds:
            logger.warning("No feeds configured")
            return

        logger.info(
            "Starting RSS to Discord with %d feeds; refresh interval %.1f seconds",
            len(self._config.feeds),
            self._config.refresh_interval,
        )
        while not self._shutdown_requested:
            self._run_feed_cycle()

        logger.info("Shutdown complete")

    def _run_feed_cycle(self) -> None:
        for feed_index, feed in enumerate(self._config.feeds):
            if self._shutdown_requested:
                break
            self._process_feed_safely(feed)
            if self._inter_feed_sleep_was_interrupted(feed_index):
                break

        if not self._shutdown_requested:
            logger.info(
                "Waiting %.1f seconds until next refresh",
                self._config.refresh_interval,
            )
            self._interruptible_sleep(self._config.refresh_interval)

    def _process_feed_safely(self, feed: FeedConfig) -> None:
        try:
            self.process_feed(feed)
        except FeedFetchInterruptedError:
            return
        except FeedFetchError as error:
            _log_feed_fetch_error(feed.id, error)
        except Exception:
            logger.exception("Error processing feed %s", feed.id)

    def _inter_feed_sleep_was_interrupted(self, feed_index: int) -> bool:
        has_next_feed = feed_index + 1 < len(self._config.feeds)
        if not has_next_feed or self._config.delay_between_feeds <= 0:
            return False
        return not self._interruptible_sleep(self._config.delay_between_feeds)

    def _fetch_entries(
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
    ) -> tuple[list[Any], str]:
        attempt = 0
        while True:
            try:
                return strategy.fetch_entries(feed.url)
            except FeedFetchError as error:
                attempt += 1
                if not error.retryable or attempt >= FEED_FETCH_MAX_ATTEMPTS:
                    raise
                delay = self._feed_retry_delay(error, attempt - 1)
                logger.warning(
                    "Error processing feed %s: %s; retrying in %.1f seconds",
                    feed.id,
                    error,
                    delay,
                )
                if not self._interruptible_sleep(delay):
                    raise FeedFetchInterruptedError from None

    @staticmethod
    def _feed_retry_delay(error: FeedFetchError, attempt: int) -> float:
        if error.retry_after is not None:
            return min(error.retry_after, FEED_FETCH_MAX_DELAY_SECONDS)
        backoff = min(
            FEED_FETCH_BASE_DELAY_SECONDS * (2**attempt),
            FEED_FETCH_MAX_BACKOFF_SECONDS,
        )
        return random.SystemRandom().uniform(0, backoff)

    def _is_too_old(self, entry: EntryData, feed_id: str) -> bool:
        max_age_days = self._config.max_post_age_days
        if max_age_days <= 0:
            return False
        if entry.timestamp is None:
            logger.warning(
                "Skipping entry without a timestamp in feed %s: %s",
                feed_id,
                entry.title,
            )
            return True

        try:
            published_at = datetime.fromisoformat(entry.timestamp)
        except ValueError:
            logger.warning(
                "Skipping entry with an invalid timestamp in feed %s: %s",
                feed_id,
                entry.title,
            )
            return True
        if published_at.tzinfo is None:
            logger.warning(
                "Skipping entry with a timezone-free timestamp in feed %s: %s",
                feed_id,
                entry.title,
            )
            return True
        return datetime.now(UTC) - published_at > timedelta(days=max_age_days)

    def _persist_delivery(self, feed_id: str, entry_id: EntryId) -> bool:
        while True:
            try:
                self._store.mark_delivered(feed_id, entry_id)
            except sqlite3.Error as error:
                error_code = getattr(error, "sqlite_errorcode", None)
                if (
                    error_code is None
                    or error_code & 0xFF not in SQLITE_TRANSIENT_ERROR_CODES
                ):
                    raise
                logger.warning(
                    "Could not persist delivery for feed %s; retrying in %.1f seconds (%s)",
                    feed_id,
                    PERSISTENCE_RETRY_DELAY_SECONDS,
                    type(error).__name__,
                )
                if not self._interruptible_sleep(PERSISTENCE_RETRY_DELAY_SECONDS):
                    return False
            else:
                return True

    def _interruptible_sleep(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self._shutdown_requested and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))
        return not self._shutdown_requested
