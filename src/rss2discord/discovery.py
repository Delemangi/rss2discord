import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal, Protocol

from .adapters import AdapterError, SourceAdapter
from .configuration import AppConfig, FeedConfig
from .delivery_store import DeliveryStore
from .discord.client import DiscordDeliveryResult, DiscordSender, WebhookMessage
from .discord.discovery_digest import build_discovery_digest
from .models import EntryData, EntryId, HealthUpdate
from .retries import (
    FetchRetryPolicy,
    SQLiteRetryInterruptedError,
    SQLiteRetryPolicy,
)
from .runtime.health import record_runtime_health, safe_error_cause
from .transports import FeedFetchError, ScraperStrategy

logger = logging.getLogger("rss2discord.app")
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


class DiscoveryRuntime(Protocol):
    """Runtime controls the discovery pipeline needs, without its wiring."""

    def sleep(self, seconds: float) -> bool: ...

    def is_shutdown_requested(self) -> bool: ...


class DiscoveryProcessor:
    """Run one feed's discovery, baseline, dedupe, delivery, and health flow."""

    def __init__(
        self,
        config: AppConfig,
        store: DeliveryStore,
        sender: DiscordSender,
        adapters: dict[str, SourceAdapter],
        runtime: DiscoveryRuntime,
    ) -> None:
        self._config = config
        self._store = store
        self._sender = sender
        self._adapters = adapters
        self._runtime = runtime

    def is_shutdown_requested(self) -> bool:
        return self._runtime.is_shutdown_requested()

    def process(self, feed: FeedConfig, strategy: ScraperStrategy) -> None:
        attempted_at = int(time.time())
        started_at = time.monotonic()
        counters = DiscoveryScanCounters()
        logger.info("Processing feed %s with strategy %s", feed.id, feed.strategy)
        entries, fetched_source_title = self.fetch_entries(feed, strategy)
        counters.entries = len(entries)
        if self.is_shutdown_requested():
            self._log_discovery_scan(feed, counters, "interrupted", started_at)
            return
        if not self._process_scan_entries(
            feed,
            strategy,
            entries,
            fetched_source_title,
            counters=counters,
            attempted_at=attempted_at,
            started_at=started_at,
        ):
            return
        if self.is_shutdown_requested():
            self._log_discovery_scan(feed, counters, "interrupted", started_at)
            return
        health_state, health_cause = self._health_state(feed, strategy, entries)
        self._record_scan_health(
            feed,
            entries,
            attempted_at=attempted_at,
            started_at=started_at,
            health_state=health_state,
            health_cause=health_cause,
        )
        self._log_discovery_scan(feed, counters, health_state, started_at)

    def _process_scan_entries(
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
        entries: list[Any],
        fetched_source_title: str,
        *,
        counters: DiscoveryScanCounters,
        attempted_at: int,
        started_at: float,
    ) -> bool:
        """Run baseline seeding and delivery; return False once a terminal state is logged."""
        baseline_state = self._prepare_complete_baseline(feed, strategy, entries)
        if baseline_state == "initialized":
            counters.seeded = True
        if baseline_state != "ready":
            return True
        outcome = self._process_entries(
            feed,
            strategy,
            entries,
            fetched_source_title,
            counters,
        )
        if outcome is EntryProcessingOutcome.INTERRUPTED:
            self._log_discovery_scan(feed, counters, "interrupted", started_at)
            return False
        if outcome is EntryProcessingOutcome.DELIVERY_FAILED:
            self._record_delivery_failure(feed, entries, attempted_at, started_at)
            self._log_discovery_scan(feed, counters, "failed", started_at)
            return False
        return True

    def _health_state(
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
        entries: list[Any],
    ) -> tuple[str, str | None]:
        if not entries:
            return "empty", None
        if feed.stale_after_days is None:
            return "healthy", None
        newest_at = self._newest_entry_at(strategy, entries)
        if newest_at is None:
            return "healthy", None
        if newest_at < datetime.now(UTC) - timedelta(days=feed.stale_after_days):
            return "stale", "NoRecentEntries"
        return "healthy", None

    def _record_delivery_failure(
        self,
        feed: FeedConfig,
        entries: list[Any],
        attempted_at: int,
        started_at: float,
    ) -> None:
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

    def _record_scan_health(
        self,
        feed: FeedConfig,
        entries: list[Any],
        *,
        attempted_at: int,
        started_at: float,
        health_state: str,
        health_cause: str | None,
    ) -> None:
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

    @staticmethod
    def _newest_entry_at(
        strategy: ScraperStrategy,
        entries: list[Any],
    ) -> datetime | None:
        parsed_timestamps: list[datetime] = []
        for entry in entries:
            timestamp = strategy.get_entry_timestamp(entry)
            if timestamp is None:
                continue
            try:
                parsed = datetime.fromisoformat(timestamp)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            parsed_timestamps.append(parsed)
        return max(parsed_timestamps, default=None)

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
        pending = self._collect_pending_entries(feed, strategy, entries, counters)
        if pending is None:
            return EntryProcessingOutcome.INTERRUPTED
        counters.pending = len(pending)
        if not pending:
            return EntryProcessingOutcome.COMPLETED
        return self._deliver_pending_entries(feed, source_title, pending, counters)

    def _collect_pending_entries(
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
        entries: list[Any],
        counters: DiscoveryScanCounters,
    ) -> list[tuple[EntryId, EntryData]] | None:
        """Collect entries worth delivering, or None when shutdown interrupted the scan."""
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
                return None

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
            if self._below_score(feed, entry_data):
                counters.below_score += 1
                continue
            if self._entry_is_too_old(feed, strategy, entry_data):
                counters.too_old += 1
                continue

            pending.append((entry_id, entry_data))
        return pending

    @staticmethod
    def _below_score(feed: FeedConfig, entry_data: EntryData) -> bool:
        """Return whether a known enrichment score fails the feed's threshold."""
        return (
            feed.hackernews_min_score is not None
            and entry_data.source_score is not None
            and entry_data.source_score < feed.hackernews_min_score
        )

    def _entry_is_too_old(
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
        entry_data: EntryData,
    ) -> bool:
        return self._is_too_old(
            entry_data,
            feed.id,
            allow_missing_timestamp=strategy.allow_missing_timestamp_after_complete_baseline
            and self._store.is_feed_initialized(feed.id),
        )

    def _deliver_pending_entries(
        self,
        feed: FeedConfig,
        source_title: str,
        pending: list[tuple[EntryId, EntryData]],
        counters: DiscoveryScanCounters,
    ) -> EntryProcessingOutcome:
        digest_threshold = feed.digest_threshold
        if digest_threshold is None or len(pending) <= digest_threshold:
            return self._deliver_entries_individually(
                feed,
                source_title,
                pending,
                counters,
            )
        counters.digest = True
        outcome = self._deliver_discovery_digest(feed, pending, source_title)
        if outcome is EntryProcessingOutcome.COMPLETED:
            counters.delivered = len(pending)
        elif outcome is EntryProcessingOutcome.DELIVERY_FAILED:
            counters.failed = len(pending)
        return outcome

    def _deliver_entries_individually(
        self,
        feed: FeedConfig,
        source_title: str,
        pending: list[tuple[EntryId, EntryData]],
        counters: DiscoveryScanCounters,
    ) -> EntryProcessingOutcome:
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
        if not self.persist_deliveries(
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
        if not self.persist_delivery(feed.id, entry_id):
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

    def fetch_entries(
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

    def persist_delivery(self, feed_id: str, entry_id: EntryId) -> bool:
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

    def persist_deliveries(self, feed_id: str, entry_ids: tuple[EntryId, ...]) -> bool:
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
