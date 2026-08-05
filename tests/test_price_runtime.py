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
from rss2discord.transports.setec_price_monitor import SetecPriceMonitorDependencies
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock, RecordingMonitor


class FailingMonitor:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def scan(self) -> None:
        raise self._error


def test_build_price_jobs_omits_reklama5_feed(tmp_path: Path) -> None:
    config = AppConfig(
        feeds=(
            FeedConfig(
                id="reklama5",
                url="https://reklama5.mk/Search?cat=584",
                webhook="https://discord.example.test/reklama5",
                strategy="reklama5",
            ),
        ),
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            config,
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=lambda _seconds: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
        )

    assert jobs == ()


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


def make_setec_feed(feed_id: str, interval: float | None) -> FeedConfig:
    return FeedConfig(
        id=feed_id,
        url=f"https://setec.example.test/e-prodazba/{feed_id}",
        webhook=f"https://discord.example.test/webhooks/{feed_id}",
        strategy="setec",
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
            anhoch_monitor_factory=monitor_factory,
        )

    # Then
    assert constructed_feed_ids == ["first", "second"]
    assert [job.interval for job in jobs] == [5, 7]
    assert all(
        dependencies.delivery.is_shutdown_requested()
        for dependencies in constructed_dependencies
    )


def test_build_price_jobs_dispatches_mixed_sources_in_feed_order(
    tmp_path: Path,
) -> None:
    # Given
    clock = FakeClock(maximum_sleeps=1)
    constructed_sources: list[str] = []
    anhoch_dependencies: list[AnhochPriceMonitorDependencies] = []
    neksio_dependencies: list[NeksioPriceMonitorDependencies] = []
    setec_dependencies: list[SetecPriceMonitorDependencies] = []
    config = AppConfig(
        feeds=(
            make_anhoch_feed("anhoch", 5),
            FeedConfig(
                id="ordinary",
                url="https://example.test/feed.xml",
                webhook="https://discord.example.test/ordinary",
            ),
            make_neksio_feed("neksio", 6),
            make_setec_feed("setec", 7),
        ),
    )

    def anhoch_monitor_factory(
        feed: FeedConfig,
        dependencies: AnhochPriceMonitorDependencies,
    ) -> RecordingMonitor:
        anhoch_dependencies.append(dependencies)
        constructed_sources.append(f"anhoch:{feed.id}")
        return RecordingMonitor(feed.id, [], clock)

    def setec_monitor_factory(
        feed: FeedConfig,
        dependencies: SetecPriceMonitorDependencies,
    ) -> RecordingMonitor:
        setec_dependencies.append(dependencies)
        constructed_sources.append(f"setec:{feed.id}")
        return RecordingMonitor(feed.id, [], clock)

    def neksio_monitor_factory(
        feed: FeedConfig,
        dependencies: NeksioPriceMonitorDependencies,
    ) -> RecordingMonitor:
        neksio_dependencies.append(dependencies)
        constructed_sources.append(f"neksio:{feed.id}")
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
            anhoch_monitor_factory=anhoch_monitor_factory,
            neksio_monitor_factory=neksio_monitor_factory,
            setec_monitor_factory=setec_monitor_factory,
        )

    # Then
    assert constructed_sources == ["anhoch:anhoch", "neksio:neksio", "setec:setec"]
    assert [job.interval for job in jobs] == [5, 6, 7]
    assert len(anhoch_dependencies) == 1
    assert len(neksio_dependencies) == 1
    assert len(setec_dependencies) == 1


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
            anhoch_monitor_factory=monitor_factory,
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
