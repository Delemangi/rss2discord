import logging
import sqlite3
from pathlib import Path

import pytest

from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.anhoch.prices import AnhochCatalog
from rss2discord.runtime.prices import PriceJobDependencies, build_price_jobs
from rss2discord.runtime.scheduler import (
    RuntimeScheduler,
    ScheduledJob,
    SchedulerControl,
    SchedulerJobs,
)
from rss2discord.transports.price_monitor import PriceMonitorDependencies
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock, RecordingMonitor

AnhochPriceMonitorDependencies = PriceMonitorDependencies[AnhochCatalog]


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


@pytest.mark.parametrize(
    ("feed_id", "failure"),
    [
        ("fetch-failed", FeedFetchError("Anhoch", "NetworkError")),
        ("persistence-failed", sqlite3.OperationalError("database is locked")),
        (
            "unexpected-failed",
            RuntimeError("https://catalog.example.test?feed_secret=hidden"),
        ),
    ],
)
def test_price_job_failure_is_sanitized_and_does_not_stop_later_jobs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    feed_id: str,
    failure: Exception,
) -> None:
    # Given
    clock = FakeClock(maximum_sleeps=2)
    events: list[tuple[str, float]] = []
    config = AppConfig(
        refresh_interval=1,
        feeds=(
            make_anhoch_feed(feed_id, 5),
            make_anhoch_feed("healthy", 5),
        ),
    )

    def monitor_factory(
        feed: FeedConfig,
        dependencies: AnhochPriceMonitorDependencies,
    ) -> FailingMonitor | RecordingMonitor:
        del dependencies
        if feed.id == feed_id:
            return FailingMonitor(failure)
        return RecordingMonitor("price-healthy", events, clock)

    caplog.set_level(logging.ERROR)
    with DeliveryStore(tmp_path / "state.db") as store:
        monkeypatch.setattr(
            "rss2discord.providers.registry.AnhochPriceMonitor",
            monitor_factory,
        )
        price_jobs = build_price_jobs(
            config,
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=clock.sleep,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
        )
        scheduler = RuntimeScheduler(
            SchedulerJobs(
                ordinary=(
                    ScheduledJob(
                        "ordinary",
                        "ordinary",
                        1,
                        lambda: events.append(("ordinary", clock.now)),
                    ),
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
    assert feed_id in caplog.text
    assert "feed_secret" not in caplog.text
    assert "hidden" not in caplog.text
