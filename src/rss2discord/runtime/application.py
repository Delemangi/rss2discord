"""Own application shutdown and assemble the sequential runtime schedule."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from functools import partial

from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordSender
from rss2discord.providers.pazar3.pacing import Pazar3RequestPacer
from rss2discord.retries import FeedFetchInterruptedError, SQLiteRetryInterruptedError

from .health import record_fetch_failure
from .prices import PriceJobDependencies, build_price_jobs
from .scheduler import (
    JobOutcome,
    JobTiming,
    RuntimeScheduler,
    ScheduledJob,
    SchedulerControl,
    SchedulerJobs,
)

logger = logging.getLogger("rss2discord.app")


class ApplicationRuntime:
    def __init__(self) -> None:
        self._shutdown_requested = False
        self.pazar3_pacer: Pazar3RequestPacer = Pazar3RequestPacer(time.monotonic)

    def request_shutdown(self) -> None:
        logger.info("Shutdown requested")
        self._shutdown_requested = True

    def is_shutdown_requested(self) -> bool:
        return self._shutdown_requested

    def sleep(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self._shutdown_requested and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))
        return not self._shutdown_requested

    def run(
        self,
        config: AppConfig,
        store: DeliveryStore,
        sender: DiscordSender,
        process_feed: Callable[[FeedConfig], JobOutcome | None],
    ) -> None:
        if not config.feeds:
            logger.warning("No feeds configured")
            return

        logger.info(
            "Starting RSS to Discord with %d feeds; refresh interval %.1f seconds",
            len(config.feeds),
            config.refresh_interval,
        )
        ordinary_intervals = tuple(
            feed.ordinary_check_interval
            if feed.ordinary_check_interval is not None
            else config.refresh_interval
            for feed in config.feeds
        )
        gap_utilization = config.delay_between_feeds * sum(
            1 / interval for interval in ordinary_intervals
        )
        if gap_utilization >= 1:
            logger.warning(
                "Ordinary-feed gap budget utilization %.3f meets or exceeds "
                "available cadence capacity; cadence is best-effort and fetch "
                "durations and price jobs can add further delay",
                gap_utilization,
            )

        jobs = SchedulerJobs(
            tuple(
                ScheduledJob(
                    feed.id,
                    "ordinary",
                    interval,
                    partial(_process_feed_safely, feed, process_feed, store),
                )
                for feed, interval in zip(
                    config.feeds,
                    ordinary_intervals,
                    strict=True,
                )
            ),
            build_price_jobs(
                config,
                PriceJobDependencies(
                    store=store,
                    sender=sender,
                    sleep=self.sleep,
                    delay_between_posts=config.delay_between_posts,
                    is_shutdown_requested=self.is_shutdown_requested,
                    pazar3_pacer=self.pazar3_pacer,
                ),
            ),
            ordinary_gap=config.delay_between_feeds,
        )
        RuntimeScheduler(
            jobs,
            SchedulerControl(
                time.monotonic,
                self.sleep,
                self.is_shutdown_requested,
            ),
            observer=partial(_record_job_timing, store),
        ).run()
        logger.info("Shutdown complete")


def _process_feed_safely(
    feed: FeedConfig,
    process_feed: Callable[[FeedConfig], JobOutcome | None],
    store: DeliveryStore,
) -> JobOutcome | None:
    attempted_at = int(time.time())
    started_at = time.monotonic()
    try:
        return process_feed(feed)
    except (FeedFetchInterruptedError, SQLiteRetryInterruptedError):
        return JobOutcome.ATTEMPTED
    except Exception as error:
        record_fetch_failure(
            store,
            feed.id,
            "ordinary",
            error,
            attempted_at,
            duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
        )
        return JobOutcome.ATTEMPTED


def _record_job_timing(store: DeliveryStore, timing: JobTiming) -> None:
    try:
        store.record_job_timing(
            feed_id=timing.name,
            job_kind=timing.kind,
            duration_ms=max(
                0,
                int((timing.finished_at - timing.started_at) * 1000),
            ),
            scheduler_lag_ms=max(
                0,
                int((timing.started_at - timing.scheduled_at) * 1000),
            ),
        )
    except sqlite3.Error as error:
        logger.log(
            logging.ERROR,
            "Job timing persistence failed for feed %s (%s)",
            timing.name,
            type(error).__name__,
        )
