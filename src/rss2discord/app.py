import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from .adapters import AdapterError, HackerNewsAdapter, RedditAdapter, SourceAdapter
from .configuration import AppConfig, FeedConfig
from .delivery_store import DeliveryStore
from .discord.client import DiscordSender, WebhookMessage
from .models import EntryData, EntryId
from .price_runtime import PriceJobDependencies, build_price_jobs
from .retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryInterruptedError,
    SQLiteRetryPolicy,
)
from .scheduler import RuntimeScheduler, ScheduledJob, SchedulerControl, SchedulerJobs
from .transports import (
    AnhochStrategy,
    FeedFetchError,
    ITMkOglasnikStrategy,
    RSSStrategy,
    ScraperStrategy,
    XenForoStrategy,
)

logger = logging.getLogger(__name__)
MAX_HACKER_NEWS_ENRICHMENTS_PER_FEED: Final = 5


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
            "anhoch": AnhochStrategy(),
            "itmk_oglasnik": ITMkOglasnikStrategy(),
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

    def is_shutdown_requested(self) -> bool:
        return self._shutdown_requested

    def process_feed(self, feed: FeedConfig) -> None:
        logger.info("Processing feed %s with strategy %s", feed.id, feed.strategy)
        strategy = self._strategies[feed.strategy]
        entries, fetched_source_title = self._fetch_entries(feed, strategy)
        if strategy.seed_existing_on_first_fetch:
            entry_ids = {
                entry_id
                for entry in entries
                if (entry_id := strategy.get_entry_id(entry)) is not None
            }
            if self._store.seed_feed(feed.id, entry_ids):
                logger.info("Initialized feed %s with existing entries", feed.id)
                return
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
        RuntimeScheduler(
            SchedulerJobs(
                ScheduledJob(self._config.refresh_interval, self._run_feed_cycle),
                build_price_jobs(
                    self._config,
                    PriceJobDependencies(
                        store=self._store,
                        sender=self._sender,
                        sleep=self._interruptible_sleep,
                        delay_between_posts=self._config.delay_between_posts,
                        is_shutdown_requested=self.is_shutdown_requested,
                    ),
                ),
            ),
            SchedulerControl(
                time.monotonic,
                self._interruptible_sleep,
                self.is_shutdown_requested,
            ),
        ).run()

        logger.info("Shutdown complete")

    def _run_feed_cycle(self) -> None:
        delay_between_feeds = self._config.delay_between_feeds
        for feed_index, feed in enumerate(self._config.feeds):
            if self._shutdown_requested:
                break
            self._process_feed_safely(feed)
            has_next_feed = feed_index < len(self._config.feeds) - 1
            should_wait = has_next_feed and delay_between_feeds > 0
            if should_wait and not self._interruptible_sleep(delay_between_feeds):
                break

    def _process_feed_safely(self, feed: FeedConfig) -> None:
        try:
            self.process_feed(feed)
        except FeedFetchInterruptedError:
            return
        except FeedFetchError as error:
            _log_feed_fetch_error(feed.id, error)
        except Exception:
            logger.exception("Error processing feed %s", feed.id)

    def _fetch_entries(
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
    ) -> tuple[list[Any], str]:
        retry_policy = FetchRetryPolicy(
            sleep=self._interruptible_sleep,
            on_retry=lambda error, delay: logger.warning(
                "Error processing feed %s: %s; retrying in %.1f seconds",
                feed.id,
                error,
                delay,
            ),
        )
        return retry_policy.execute(lambda: strategy.fetch_entries(feed.url))

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
        retry_policy = SQLiteRetryPolicy(
            sleep=self._interruptible_sleep,
            on_retry=lambda error, delay: logger.warning(
                "Could not persist delivery for feed %s; retrying in %.1f seconds (%s)",
                feed_id,
                delay,
                type(error).__name__,
            ),
        )
        try:
            retry_policy.execute(lambda: self._store.mark_delivered(feed_id, entry_id))
        except SQLiteRetryInterruptedError:
            return False
        return True

    def _interruptible_sleep(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self._shutdown_requested and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))
        return not self._shutdown_requested
