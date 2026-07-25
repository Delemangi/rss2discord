import logging
import sqlite3
from pathlib import Path

import pytest

from rss2discord.app import RSSToDiscord
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


def test_build_price_jobs_includes_only_enabled_anhoch_feeds_with_their_intervals(
    tmp_path: Path,
) -> None:
    # Given
    clock = FakeClock(maximum_sleeps=1)
    constructed_feed_ids: list[str] = []
    constructed_dependencies: list[AnhochPriceMonitorDependencies] = []
    config = AppConfig(
        feeds=(
            FeedConfig(
                id="ordinary",
                url="https://example.test/feed.xml",
                webhook="https://discord.example.test/ordinary",
            ),
            make_anhoch_feed("first", 5),
            make_anhoch_feed("second", 7),
            make_anhoch_feed("disabled", None),
        ),
    )

    def monitor_factory(
        feed: FeedConfig,
        dependencies: AnhochPriceMonitorDependencies,
    ) -> RecordingMonitor:
        constructed_dependencies.append(dependencies)
        constructed_feed_ids.append(feed.id)
        return RecordingMonitor(feed.id, [], clock)

    with DeliveryStore(tmp_path / "state.db") as store:
        dependencies = PriceJobDependencies(
            store=store,
            sender=FakeSender([]),
            sleep=clock.sleep,
            delay_between_posts=0,
            is_shutdown_requested=lambda: True,
        )

        # When
        jobs = build_price_jobs(
            config,
            dependencies,
            monitor_factory=monitor_factory,
        )

    # Then
    assert constructed_feed_ids == ["first", "second"]
    assert [job.interval for job in jobs] == [5, 7]
    assert all(
        dependencies.delivery.is_shutdown_requested()
        for dependencies in constructed_dependencies
    )


def test_run_schedules_ordinary_before_price_jobs_on_independent_cadences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    clock = FakeClock(maximum_sleeps=5)
    events: list[tuple[str, float]] = []
    config = AppConfig(
        refresh_interval=3,
        feeds=(
            FeedConfig(
                id="ordinary",
                url="https://example.test/feed.xml",
                webhook="https://discord.example.test/ordinary",
            ),
            make_anhoch_feed("first", 5),
            make_anhoch_feed("second", 7),
        ),
    )

    def fake_price_jobs(
        built_config: AppConfig,
        dependencies: PriceJobDependencies,
    ) -> tuple[ScheduledJob, ...]:
        assert built_config is config
        assert dependencies.delay_between_posts == config.delay_between_posts
        return (
            ScheduledJob(5, lambda: events.append(("price-first", clock.now))),
            ScheduledJob(7, lambda: events.append(("price-second", clock.now))),
        )

    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(config=config, store=store, sender=FakeSender([]))
        monkeypatch.setattr("rss2discord.app.build_price_jobs", fake_price_jobs)
        monkeypatch.setattr("rss2discord.app.time.monotonic", clock.monotonic)
        monkeypatch.setattr(app, "_interruptible_sleep", clock.sleep)
        monkeypatch.setattr(
            app,
            "_run_feed_cycle",
            lambda: events.append(("ordinary", clock.now)),
        )

        # When
        app.run()

    # Then
    assert events == [
        ("ordinary", 0),
        ("price-first", 0),
        ("price-second", 0),
        ("ordinary", 3),
        ("price-first", 5),
        ("ordinary", 6),
        ("price-second", 7),
    ]
    assert clock.sleep_calls == [3, 2, 1, 1, 2]


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
        price_jobs = build_price_jobs(
            config,
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=clock.sleep,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
            monitor_factory=monitor_factory,
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
    assert feed_id in caplog.text
    assert "feed_secret" not in caplog.text
    assert "hidden" not in caplog.text


def test_run_stops_after_scheduler_sleep_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    events: list[str] = []
    config = AppConfig(
        refresh_interval=3,
        feeds=(
            FeedConfig(
                id="ordinary",
                url="https://example.test/feed.xml",
                webhook="https://discord.example.test/ordinary",
            ),
        ),
    )

    def fake_price_jobs(
        built_config: AppConfig,
        dependencies: PriceJobDependencies,
    ) -> tuple[ScheduledJob, ...]:
        del built_config, dependencies
        return (ScheduledJob(5, lambda: events.append("price")),)

    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(config=config, store=store, sender=FakeSender([]))
        monkeypatch.setattr("rss2discord.app.build_price_jobs", fake_price_jobs)
        monkeypatch.setattr(app, "_run_feed_cycle", lambda: events.append("ordinary"))

        def interrupt_scheduler_sleep(_seconds: float) -> bool:
            app.request_shutdown()
            return False

        monkeypatch.setattr(app, "_interruptible_sleep", interrupt_scheduler_sleep)

        # When
        app.run()

    # Then
    assert events == ["ordinary", "price"]
