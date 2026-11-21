import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal

from .adapters import (
    AdapterError,
    HackerNewsAdapter,
    RedditAdapter,
    SourceAdapter,
    fetch_hacker_news_topstories,
)
from .configuration import AppConfig, FeedConfig
from .delivery_store import DeliveryStore
from .discord.client import DiscordDeliveryResult, DiscordSender, WebhookMessage
from .discord.discovery_digest import build_discovery_digest
from .models import EntryData, EntryId, HealthUpdate
from .providers.registry import build_discovery_strategies
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
# Filtered feeds need a score for every candidate, not just the first few.
MAX_HACKER_NEWS_ENRICHMENTS_PER_FILTERED_FEED: Final = 50


class EntryProcessingOutcome(StrEnum):
    """Whether discovery delivery completed, was interrupted, or failed."""

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    DELIVERY_FAILED = "delivery_failed"


@dataclass(slots=True)
class DiscoveryScanCounters:
    """Counters reported once per discovery scan."""

    entries: int = 0
    unhandled: int = 0
    pending: int = 0
    delivered: int = 0
    failed: int = 0
    too_old: int = 0
    below_score: int = 0
    duplicates: int = 0
    missing_id: int = 0
    seeded: bool = False
    digest: bool = False


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
            "hackernews": HackerNewsAdapter(
                fetch_topstories=fetch_hacker_news_topstories,
            ),
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
        counters = DiscoveryScanCounters()
        logger.info("Processing feed %s with strategy %s", feed.id, feed.strategy)
        strategy = self._strategies[feed.strategy]
        entries, fetched_source_title = self._fetch_entries(feed, strategy)
        counters.entries = len(entries)
        if self.is_shutdown_requested():
            self._log_discovery_scan(feed, counters, "interrupted", started_at)
            return JobOutcome.ATTEMPTED
        baseline_state = self._prepare_complete_baseline(feed, strategy, entries)
        if baseline_state == "initialized":
            counters.seeded = True
        if baseline_state == "ready":
            outcome = self._process_entries(
                feed,
                strategy,
                entries,
                fetched_source_title,
                counters,
            )
            if outcome is EntryProcessingOutcome.INTERRUPTED:
                self._log_discovery_scan(feed, counters, "interrupted", started_at)
                return JobOutcome.ATTEMPTED
            if outcome is EntryProcessingOutcome.DELIVERY_FAILED:
                record_runtime_health(
                    self._store,
                    HealthUpdate(
                        feed_id=feed.id,
                        job_kind="ordinary",
                        state="failed",
                        cause="DeliveryRetryPending",
                        attempted_at=attempted_at,
                        success=False,
                        nonempty=bool(entries),
                        item_count=len(entries),
                        duration_ms=max(
                            0,
                            int((time.monotonic() - started_at) * 1000),
                        ),
                        scheduler_lag_ms=0,
                    ),
                )
                self._log_discovery_scan(feed, counters, "failed", started_at)
                return JobOutcome.ATTEMPTED
        if self.is_shutdown_requested():
            self._log_discovery_scan(feed, counters, "interrupted", started_at)
            return JobOutcome.ATTEMPTED
        health_state = "healthy" if entries else "empty"
        health_cause: str | None = None
        if entries and feed.stale_after_days is not None:
            newest_at = self._newest_entry_at(strategy, entries)
            if newest_at is not None and newest_at < (
                datetime.now(UTC) - timedelta(days=feed.stale_after_days)
            ):
                health_state = "stale"
                health_cause = "NoRecentEntries"
        record_runtime_health(
            self._store,
            HealthUpdate(
                feed_id=feed.id,
                job_kind="ordinary",
                state=health_state,
                cause=health_cause,
                attempted_at=attempted_at,
                success=True,
                nonempty=bool(entries),
                item_count=len(entries),
                duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
                scheduler_lag_ms=0,
            ),
        )
        self._log_discovery_scan(feed, counters, health_state, started_at)
        return JobOutcome.ATTEMPTED

    @staticmethod
    def _newest_entry_at(
        strategy: ScraperStrategy,
        entries: list[Any],
    ) -> datetime | None:
        timestamp = strategy.get_entry_timestamp(entries[-1])
        if timestamp is None:
            return None
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    @staticmethod
    def _log_discovery_scan(
        feed: FeedConfig,
        counters: DiscoveryScanCounters,
        state: str,
        started_at: float,
    ) -> None:
        logger.info(
            "Discovery scan feed=%s strategy=%s entries=%d unhandled=%d "
            "pending=%d delivered=%d failed=%d too_old=%d below_score=%d "
            "duplicates=%d missing_id=%d seeded=%s digest=%s state=%s "
            "duration_ms=%d",
            feed.id,
            feed.strategy,
            counters.entries,
            counters.unhandled,
            counters.pending,
            counters.delivered,
            counters.failed,
            counters.too_old,
            counters.below_score,
            counters.duplicates,
            counters.missing_id,
            str(counters.seeded).lower(),
            str(counters.digest).lower(),
            state,
            max(0, int((time.monotonic() - started_at) * 1000)),
        )

    def _process_entries(
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
        entries: list[Any],
        fetched_source_title: str,
        counters: DiscoveryScanCounters,
    ) -> EntryProcessingOutcome:
        """Process pending entries, reporting completion, interruption, or failure."""
        if self._seed_existing_entries(feed, strategy, entries):
            counters.seeded = True
            return EntryProcessingOutcome.COMPLETED
        source_title = feed.name or fetched_source_title
        seen_entry_ids: set[EntryId] = set()
        adapter = self._adapters[feed.adapter] if feed.adapter is not None else None
        hacker_news_enrichments_remaining: int | None = None
        if feed.adapter == "hackernews":
            hacker_news_enrichments_remaining = (
                MAX_HACKER_NEWS_ENRICHMENTS_PER_FILTERED_FEED
                if feed.hackernews_min_score is not None
                else MAX_HACKER_NEWS_ENRICHMENTS_PER_FEED
            )
        enrichment_limit_logged = False
        pending: list[tuple[EntryId, EntryData]] = []

        for entry in entries:
            if self.is_shutdown_requested():
                return EntryProcessingOutcome.INTERRUPTED

            entry_id = self._unhandled_entry_id(
                feed,
                strategy,
                entry,
                seen_entry_ids,
                counters,
            )
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
            # Fail open when enrichment could not provide a score.
            if (
                feed.hackernews_min_score is not None
                and entry_data.source_score is not None
                and entry_data.source_score < feed.hackernews_min_score
            ):
                counters.below_score += 1
                continue
            if self._is_too_old(
                entry_data,
                feed.id,
                allow_missing_timestamp=strategy.allow_missing_timestamp_after_complete_baseline
                and self._store.is_feed_initialized(feed.id),
            ):
                counters.too_old += 1
                continue

            pending.append((entry_id, entry_data))

        counters.pending = len(pending)
        if not pending:
            return EntryProcessingOutcome.COMPLETED
        digest_threshold = feed.digest_threshold
        if digest_threshold is None or len(pending) <= digest_threshold:
            had_failure = False
            for entry_id, entry_data in pending:
                if self.is_shutdown_requested():
                    return EntryProcessingOutcome.INTERRUPTED
                outcome = self._deliver_entry(
                    feed,
                    entry_data,
                    source_title,
                    entry_id,
                )
                if outcome is EntryProcessingOutcome.INTERRUPTED:
                    return EntryProcessingOutcome.INTERRUPTED
                if outcome is EntryProcessingOutcome.DELIVERY_FAILED:
                    counters.failed += 1
                    had_failure = True
                else:
                    counters.delivered += 1
            return (
                EntryProcessingOutcome.DELIVERY_FAILED
                if had_failure
                else EntryProcessingOutcome.COMPLETED
            )
        counters.digest = True
        outcome = self._deliver_discovery_digest(feed, pending, source_title)
        if outcome is EntryProcessingOutcome.COMPLETED:
            counters.delivered = len(pending)
        elif outcome is EntryProcessingOutcome.DELIVERY_FAILED:
            counters.failed = len(pending)
        return outcome

    def _deliver_discovery_digest(
        self,
        feed: FeedConfig,
        pending: list[tuple[EntryId, EntryData]],
        source_title: str,
    ) -> EntryProcessingOutcome:
        if self.is_shutdown_requested():
            return EntryProcessingOutcome.INTERRUPTED
        message = build_discovery_digest(
            feed,
            pending,
            source_title,
        )
        result = self._sender.send(message, self._runtime.sleep)
        if result is DiscordDeliveryResult.INTERRUPTED:
            return EntryProcessingOutcome.INTERRUPTED
        if result is not DiscordDeliveryResult.DELIVERED:
            logger.warning(
                "Discord rejected discovery digest for feed %s; will retry",
                feed.id,
            )
            return EntryProcessingOutcome.DELIVERY_FAILED
        if not self._persist_deliveries(
            feed.id,
            tuple(entry_id for entry_id, _ in pending),
        ):
            return EntryProcessingOutcome.INTERRUPTED
        if not self._runtime.sleep(self._config.delay_between_posts):
            return EntryProcessingOutcome.INTERRUPTED
        return EntryProcessingOutcome.COMPLETED

    def _deliver_entry(
        self,
        feed: FeedConfig,
        entry: EntryData,
        source_title: str,
        entry_id: EntryId,
    ) -> EntryProcessingOutcome:
        message = WebhookMessage(feed=feed, entry=entry, source_title=source_title)
        result = self._sender.send(message, self._runtime.sleep)
        if result is DiscordDeliveryResult.INTERRUPTED:
            return EntryProcessingOutcome.INTERRUPTED
        if result is not DiscordDeliveryResult.DELIVERED:
            logger.warning(
                "Discord rejected entry delivery for feed %s; will retry",
                feed.id,
            )
            return EntryProcessingOutcome.DELIVERY_FAILED
        if not self._persist_delivery(feed.id, entry_id):
            return EntryProcessingOutcome.INTERRUPTED
        if not self._runtime.sleep(self._config.delay_between_posts):
            return EntryProcessingOutcome.INTERRUPTED
        return EntryProcessingOutcome.COMPLETED

    def _unhandled_entry_id[EntryT](
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
        entry: EntryT,
        seen_entry_ids: set[EntryId],
        counters: DiscoveryScanCounters,
    ) -> EntryId | None:
        entry_id = strategy.get_entry_id(entry)
        if entry_id is None:
            counters.missing_id += 1
            logger.warning("Skipping entry without a stable ID in feed %s", feed.id)
            return None
        is_seen = entry_id in seen_entry_ids
        seen_entry_ids.add(entry_id)

        if is_seen or self._store.has_delivered(feed.id, entry_id):
            counters.duplicates += 1
            return None

        counters.unhandled += 1
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
    ) -> Literal["ready", "initialized", "empty"]:
        if not strategy.allow_missing_timestamp_after_complete_baseline:
            return "ready"
        if self._store.is_feed_initialized(feed.id):
            return "ready"
        if not entries:
            return "empty"
        entry_ids = strategy.get_initialization_entry_ids(entries)
        if len(entry_ids) != len(entries):
            raise FeedFetchError("Feed", "InvalidBaselineEntries")
        if self._store.seed_feed(feed.id, entry_ids):
            return "initialized"
        return "ready"

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

    def _persist_deliveries(self, feed_id: str, entry_ids: tuple[EntryId, ...]) -> bool:
        retry_policy = SQLiteRetryPolicy(
            sleep=self._runtime.sleep,
            on_retry=lambda error, delay: logger.warning(
                "Could not persist deliveries for feed %s; retrying in %.1f seconds (%s)",
                feed_id,
                delay,
                type(error).__name__,
            ),
        )
        try:
            retry_policy.execute(
                lambda: self._store.mark_delivered_many(feed_id, entry_ids),
            )
        except SQLiteRetryInterruptedError:
            return False
        return True
