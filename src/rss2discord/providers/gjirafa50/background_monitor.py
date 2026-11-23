"""Gjirafa50 background worker backed by the shared price worker."""

from __future__ import annotations

import time

from rss2discord.configuration import FeedConfig
from rss2discord.providers.background import (
    BackgroundPriceMonitor,
    SharedPriceMonitorDependencies,
)
from rss2discord.providers.gjirafa50.prices import (
    Gjirafa50PriceMonitor,
    Gjirafa50PriceMonitorDependencies,
)
from rss2discord.providers.pazar3.pacing import Pazar3RequestPacer


class Gjirafa50BackgroundPriceMonitor(BackgroundPriceMonitor):
    """Serialize Gjirafa50 scans through the shared background worker."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: Gjirafa50PriceMonitorDependencies,
    ) -> None:
        shared = SharedPriceMonitorDependencies(
            snapshots=dependencies.snapshots,
            database_path=dependencies.database_path,
            sender=dependencies.sender,
            fetch_retry_policy=dependencies.fetch_retry_policy,
            sqlite_retry_policy=dependencies.sqlite_retry_policy,
            delivery=dependencies.delivery,
            pazar3_pacer=Pazar3RequestPacer(time.monotonic),
            cooldown_peer_feed_ids=dependencies.cooldown_peer_feed_ids,
        )

        def factory(
            worker_feed: FeedConfig,
            worker_dependencies: SharedPriceMonitorDependencies,
        ) -> Gjirafa50PriceMonitor:
            return Gjirafa50PriceMonitor(
                worker_feed,
                Gjirafa50PriceMonitorDependencies(
                    catalog=dependencies.catalog,
                    snapshots=worker_dependencies.snapshots,
                    sender=worker_dependencies.sender,
                    fetch_retry_policy=worker_dependencies.fetch_retry_policy,
                    sqlite_retry_policy=worker_dependencies.sqlite_retry_policy,
                    delivery=worker_dependencies.delivery,
                    database_path=worker_dependencies.database_path,
                    cooldown_peer_feed_ids=(worker_dependencies.cooldown_peer_feed_ids),
                ),
            )

        super().__init__(feed, shared, factory)
