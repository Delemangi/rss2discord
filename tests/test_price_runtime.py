import logging
import sqlite3
from pathlib import Path

import pytest

from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.scheduler import (
    RuntimeScheduler,
    ScheduledJob,
    SchedulerControl,
    SchedulerJobs,
)
from rss2discord.transports.anhoch_price_monitor import AnhochPriceMonitorDependencies
from rss2discord.transports.neksio_price_monitor import NeksioPriceMonitorDependencies
from tests.app_helpers import FakeSender


class FakeClock:
    def __init__(self, maximum_sleeps: int) -> None:
        self.now = 0.0
        self.sleep_calls: list[float] = []
        self._maximum_sleeps = maximum_sleeps

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> bool:
        self.sleep_calls.append(seconds)
        self.now += seconds
        return len(self.sleep_calls) < self._maximum_sleeps


class RecordingMonitor:
    def __init__(
        self,
        feed_id: str,
        events: list[tuple[str, float]],
        clock: FakeClock,
    ) -> None:
        self._feed_id = feed_id
        self._events = events
        self._clock = clock

    def scan(self) -> None:
        self._events.append((self._feed_id, self._clock.now))


class FailingMonitor:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def scan(self) -> None:
        raise self._error


def make_anhoch_feed(feed_id: str, interval: float | None) -> FeedConfig:
    return FeedConfig(
        id=feed_id,
        url=f"https://catalog.example.test/{feed_id}?feed_secret=hidden",
        webhook=f"https://discord.example.test/webhooks/{feed_id}/hidden",
        strategy="anhoch",
        price_check_interval=interval,
    )


def make_neksio_feed(feed_id: str, interval: float | None) -> FeedConfig:
    return FeedConfig(
        id=feed_id,
        url=f"https://g.store.neksio.mk/{feed_id}",
        webhook=f"https://discord.example.test/webhooks/{feed_id}/hidden",
        strategy="neksio",
        price_check_interval=interval,
    )


@pytest.mark.parametrize(
    ("feed", "healthy_feed", "failure"),
    [
        (
            make_anhoch_feed("fetch-failed", 5),
            make_anhoch_feed("healthy", 5),
            FeedFetchError("Anhoch", "NetworkError"),
        ),
        (
            make_anhoch_feed("persistence-failed", 5),
            make_anhoch_feed("healthy", 5),
            sqlite3.OperationalError("database is locked"),
        ),
        (
            make_anhoch_feed("unexpected-failed", 5),
            make_anhoch_feed("healthy", 5),
            RuntimeError("https://catalog.example.test?feed_secret=hidden"),
        ),
        (
            make_neksio_feed("neksio-failed", 5),
            make_neksio_feed("healthy-neksio", 5),
            FeedFetchError("Neksio", "NetworkError"),
        ),
    ],
)
def test_price_job_failure_is_sanitized_and_does_not_stop_later_jobs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    feed: FeedConfig,
    healthy_feed: FeedConfig,
    failure: Exception,
) -> None:
    # Given
    clock = FakeClock(maximum_sleeps=2)
    events: list[tuple[str, float]] = []
    config = AppConfig(refresh_interval=1, feeds=(feed, healthy_feed))

    def anhoch_monitor_factory(
        feed: FeedConfig,
        dependencies: AnhochPriceMonitorDependencies,
    ) -> FailingMonitor | RecordingMonitor:
        del dependencies
        if feed.id == config.feeds[0].id:
            return FailingMonitor(failure)
        return RecordingMonitor("price-healthy", events, clock)

    def neksio_monitor_factory(
        feed: FeedConfig,
        dependencies: NeksioPriceMonitorDependencies,
    ) -> FailingMonitor | RecordingMonitor:
        del dependencies
        if feed.id == config.feeds[0].id:
            return FailingMonitor(failure)
        return RecordingMonitor("price-healthy", events, clock)

    caplog.set_level(logging.ERROR)
    with DeliveryStore(tmp_path / "state.db") as store:
        price_jobs = build_price_jobs(
            config,
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=clock.sleep,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
            anhoch_monitor_factory=anhoch_monitor_factory,
            neksio_monitor_factory=neksio_monitor_factory,
        )
        scheduler = RuntimeScheduler(
            SchedulerJobs(
                ordinary=ScheduledJob(
                    1,
                    lambda: events.append(("ordinary", clock.now)),
                ),
                prices=price_jobs,
            ),
            SchedulerControl(
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                is_shutdown_requested=lambda: False,
            ),
        )

        # When
        scheduler.run()

    # Then
    assert events == [
        ("ordinary", 0),
        ("price-healthy", 0),
        ("ordinary", 1),
    ]
    assert feed.id in caplog.text
    assert "feed_secret" not in caplog.text
    assert "hidden" not in caplog.text
