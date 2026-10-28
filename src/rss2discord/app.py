import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from .adapters import AdapterError, HackerNewsAdapter, RedditAdapter, SourceAdapter
from .configuration import AppConfig, FeedConfig
from .delivery_limits import enforce_delivery_limits
from .delivery_store import DeliveryStore
from .discord.client import DiscordSender, WebhookMessage
from .discovery_builders import build_discovery_strategies
from .models import EntryData, EntryId
from .recovery_models import HealthUpdate
from .retries import (
    FetchRetryPolicy,
    SQLiteRetryInterruptedError,
    SQLiteRetryPolicy,
)
from .runtime.application import ApplicationRuntime
from .runtime.health import (
    feed_is_blocked,
    record_runtime_health,
    safe_error_cause,
)
from .runtime.scheduler import JobOutcome
from .transports import FeedFetchError, ScraperStrategy

logger = logging.getLogger(__name__)
MAX_HACKER_NEWS_ENRICHMENTS_PER_FEED: Final = 5


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
        self._runtime = ApplicationRuntime()
        self._strategies = build_discovery_strategies(
            is_shutdown_requested=self.is_shutdown_requested,
            sleep=self._runtime.sleep,
            pazar3_pacer=self._runtime.pazar3_pacer,
        )
        self._adapters: dict[str, SourceAdapter] = {
            "hackernews": HackerNewsAdapter(),
            "reddit": RedditAdapter(),
        }

    def request_shutdown(self) -> None:
        self._runtime.request_shutdown()

    def is_shutdown_requested(self) -> bool:
        return self._runtime.is_shutdown_requested()

    def process_feed(self, feed: FeedConfig) -> JobOutcome:
        if self.is_shutdown_requested():
            return JobOutcome.SKIPPED
        attempted_at = int(time.time())
        if feed_is_blocked(feed, self._config.feeds, self._store, attempted_at):
            return JobOutcome.SKIPPED
        started_at = time.monotonic()
        logger.info("Processing feed %s with strategy %s", feed.id, feed.strategy)
        strategy = self._strategies[feed.strategy]
        entries, fetched_source_title = self._fetch_entries(feed, strategy)
        if self.is_shutdown_requested():
            return JobOutcome.ATTEMPTED
        baseline_state = self._prepare_complete_baseline(feed, strategy, entries)
        if baseline_state == "ready" and not self._process_entries(
            feed,
            strategy,
            entries,
            fetched_source_title,
        ):
            return JobOutcome.ATTEMPTED
        if self.is_shutdown_requested():
            return JobOutcome.ATTEMPTED
        health_state = "healthy" if entries else "empty"
        if baseline_state == "recovery_required":
            health_state = "recovery_required"
        record_runtime_health(
            self._store,
            HealthUpdate(
                feed_id=feed.id,
                job_kind="ordinary",
                state=health_state,
                cause="CompleteBaselineRequired"
                if baseline_state == "recovery_required"
                else None,
                attempted_at=attempted_at,
                success=True,
                nonempty=bool(entries),
                item_count=len(entries),
                duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
                scheduler_lag_ms=0,
            ),
        )
        return JobOutcome.ATTEMPTED

    def _process_entries(
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
        entries: list[Any],
        fetched_source_title: str,
    ) -> bool:
        """Return whether processing completed rather than being interrupted."""
        if self._seed_existing_entries(feed, strategy, entries):
            return True
        enforce_delivery_limits(feed.id, entries, strategy, self._store)
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
            if self.is_shutdown_requested():
                return False

            entry_id = self._unhandled_entry_id(feed, strategy, entry, seen_entry_ids)
            if entry_id is None:
                continue
            entry_data = strategy.get_entry_data(entry)
            entry_data, hacker_news_enrichments_remaining, enrichment_limit_logged = (
                self._adapt_entry(
                    feed,
                    adapter,
                    entry,
                    entry_data,
                    hacker_news_enrichments_remaining,
                    enrichment_limit_logged=enrichment_limit_logged,
                )
            )
            if self._is_too_old(
                entry_data,
                feed.id,
                allow_missing_timestamp=strategy.allow_missing_timestamp_after_complete_baseline
                and self._store.has_complete_baseline(feed.id),
            ):
                continue

            if not self._deliver_entry(feed, entry_data, source_title, entry_id):
                return False
        return True

    def _deliver_entry(
        self,
        feed: FeedConfig,
        entry: EntryData,
        source_title: str,
        entry_id: EntryId,
    ) -> bool:
        message = WebhookMessage(feed=feed, entry=entry, source_title=source_title)
        if not self._sender.send(message, self._runtime.sleep):
            return True
        if not self._persist_delivery(feed.id, entry_id):
            return False
        return self._runtime.sleep(self._config.delay_between_posts)

    def _unhandled_entry_id[EntryT](
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
        entry: EntryT,
        seen_entry_ids: set[EntryId],
    ) -> EntryId | None:
        entry_id = strategy.get_entry_id(entry)
        if entry_id is None:
            logger.warning("Skipping entry without a stable ID in feed %s", feed.id)
            return None
        is_seen = entry_id in seen_entry_ids
        seen_entry_ids.add(entry_id)

        if is_seen or self._store.has_handled_entry(feed.id, entry_id):
            return None

        return entry_id

    @staticmethod
    def _adapt_entry[EntryT](
        feed: FeedConfig,
        adapter: SourceAdapter | None,
        entry: EntryT,
        entry_data: EntryData,
        hacker_news_enrichments_remaining: int | None,
        *,
        enrichment_limit_logged: bool,
    ) -> tuple[EntryData, int | None, bool]:
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
                "Hacker News enrichment limit reached for feed %s; using baseline data",
                feed.id,
            )
            enrichment_limit_logged = True
        return entry_data, hacker_news_enrichments_remaining, enrichment_limit_logged

    def _seed_existing_entries(
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
        entries: list[Any],
    ) -> bool:
        should_seed_existing = (
            feed.seed_existing_on_first_fetch or strategy.seed_existing_on_first_fetch
        )
        if should_seed_existing:
            entry_ids = strategy.get_initialization_entry_ids(entries)
            if not entry_ids and (
                feed.seed_existing_on_first_fetch
                or strategy.require_entries_for_initialization
            ):
                return True
            if self._store.seed_feed(feed.id, entry_ids):
                logger.info("Initialized feed %s with existing entries", feed.id)
                return True
        return False

    def run(self) -> None:
        self._runtime.run(self._config, self._store, self._sender, self.process_feed)

    def _prepare_complete_baseline(
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
        entries: list[Any],
    ) -> Literal["ready", "initialized", "recovery_required", "empty"]:
        if not strategy.allow_missing_timestamp_after_complete_baseline:
            return "ready"
        if self._store.has_complete_baseline(feed.id):
            return "ready"
        if not entries:
            return "empty"
        entry_ids = strategy.get_initialization_entry_ids(entries)
        if len(entry_ids) != len(entries):
            raise FeedFetchError("Feed", "InvalidBaselineEntries")
        if self._store.is_feed_initialized(feed.id):
            self._store.record_feed_baseline_candidate(
                feed_id=feed.id,
                entry_ids=entry_ids,
                reason="Complete catalog recovery baseline required",
            )
            return "recovery_required"
        self._store.initialize_feed_with_baseline(
            feed_id=feed.id,
            entry_ids=entry_ids,
            reason="Initial complete catalog baseline",
        )
        return "initialized"

    def _fetch_entries(
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
    ) -> tuple[list[Any], str]:
        retry_policy = FetchRetryPolicy(
            sleep=self._runtime.sleep,
            on_retry=lambda error, delay: logger.warning(
                "Error processing feed %s: %s; retrying in %.1f seconds",
                feed.id,
                safe_error_cause(error),
                delay,
            ),
        )
        return retry_policy.execute(lambda: strategy.fetch_entries(feed.url))

    def _is_too_old(
        self,
        entry: EntryData,
        feed_id: str,
        *,
        allow_missing_timestamp: bool = False,
    ) -> bool:
        max_age_days = self._config.max_post_age_days
        if max_age_days <= 0:
            return False
        if entry.timestamp is None:
            if allow_missing_timestamp:
                return False
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
            sleep=self._runtime.sleep,
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
