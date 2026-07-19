import logging
import sqlite3
import time
from datetime import UTC, datetime, timedelta

from configuration import AppConfig, FeedConfig
from delivery_store import DeliveryStore
from discord_client import DiscordSender, WebhookMessage
from models import EntryData, EntryId
from strategies import FeedFetchError, RSSStrategy, ScraperStrategy, XenForoStrategy

logger = logging.getLogger(__name__)
PERSISTENCE_RETRY_DELAY_SECONDS = 5.0


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
        self._shutdown_requested = False

    def request_shutdown(self) -> None:
        logger.info("Shutdown requested")
        self._shutdown_requested = True

    def process_feed(self, feed: FeedConfig) -> None:
        logger.info("Processing feed %s with strategy %s", feed.id, feed.strategy)
        strategy = self._strategies[feed.strategy]
        entries, fetched_source_title = strategy.fetch_entries(feed.url)
        source_title = feed.name or fetched_source_title
        seen_entry_ids: set[EntryId] = set()

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
            if self._is_too_old(entry_data, feed.id):
                continue

            message = WebhookMessage(
                feed=feed,
                entry=entry_data,
                source_title=source_title,
            )
            if not self._sender.send(message, self._interruptible_sleep):
                continue

            self._persist_delivery(feed.id, entry_id)
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
            for feed_index, feed in enumerate(self._config.feeds):
                if self._shutdown_requested:
                    break
                try:
                    self.process_feed(feed)
                except FeedFetchError as error:
                    _log_feed_fetch_error(feed.id, error)
                except Exception:
                    logger.exception("Error processing feed %s", feed.id)

                has_next_feed = feed_index + 1 < len(self._config.feeds)
                if (
                    has_next_feed
                    and self._config.delay_between_feeds > 0
                    and not self._interruptible_sleep(
                        self._config.delay_between_feeds,
                    )
                ):
                    break

            if not self._shutdown_requested:
                logger.info(
                    "Waiting %.1f seconds until next refresh",
                    self._config.refresh_interval,
                )
                self._interruptible_sleep(self._config.refresh_interval)

        logger.info("Shutdown complete")

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

    def _persist_delivery(self, feed_id: str, entry_id: EntryId) -> None:
        while True:
            try:
                self._store.mark_delivered(feed_id, entry_id)
            except sqlite3.Error as error:
                logger.warning(
                    "Could not persist delivery for feed %s; retrying in %.1f seconds (%s)",
                    feed_id,
                    PERSISTENCE_RETRY_DELAY_SECONDS,
                    type(error).__name__,
                )
                time.sleep(PERSISTENCE_RETRY_DELAY_SECONDS)
            else:
                return

    def _interruptible_sleep(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self._shutdown_requested and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))
        return not self._shutdown_requested
