"""Application wiring for discovery feeds and the scheduler runtime."""

import logging
import time
from typing import Any

from .adapters import (
    HackerNewsAdapter,
    RedditAdapter,
    SourceAdapter,
    fetch_hacker_news_topstories,
)
from .configuration import AppConfig, FeedConfig
from .delivery_store import DeliveryStore
from .discord.client import DiscordSender
from .discovery import DiscoveryProcessor
from .models import EntryId
from .providers.registry import build_discovery_strategies
from .runtime.application import ApplicationRuntime
from .runtime.health import feed_is_blocked
from .runtime.scheduler import JobOutcome
from .transports import ScraperStrategy

logger = logging.getLogger(__name__)


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
        self._discovery = DiscoveryProcessor(
            config=self._config,
            store=self._store,
            sender=self._sender,
            adapters=self._adapters,
            runtime=self._runtime,
        )

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
        strategy = self._strategies.get(feed.strategy)
        if strategy is None:
            logger.warning(
                "No discovery strategy for feed %s; skipping ordinary scan",
                feed.id,
            )
            return JobOutcome.SKIPPED
        self._discovery.process(feed, strategy)
        return JobOutcome.ATTEMPTED

    def run(self) -> None:
        self._runtime.run(self._config, self._store, self._sender, self.process_feed)

    # Compatibility delegates for tests and callers that exercise the
    # discovery primitives directly.
    def _fetch_entries(
        self,
        feed: FeedConfig,
        strategy: ScraperStrategy,
    ) -> tuple[list[Any], str]:
        return self._discovery.fetch_entries(feed, strategy)

    def _persist_delivery(self, feed_id: str, entry_id: EntryId) -> bool:
        return self._discovery.persist_delivery(feed_id, entry_id)

    def _persist_deliveries(
        self,
        feed_id: str,
        entry_ids: tuple[EntryId, ...],
    ) -> bool:
        return self._discovery.persist_deliveries(feed_id, entry_ids)
