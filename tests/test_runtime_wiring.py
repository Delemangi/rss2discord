from functools import partial
from pathlib import Path

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.scheduler import ScheduledJob
from rss2discord.transports.anhoch_catalog import AnhochCatalogClient
from rss2discord.transports.anhoch_price_monitor import (
    AnhochPriceMonitor,
    AnhochPriceMonitorDependencies,
)
from rss2discord.transports.neksio_catalog import NeksioCatalogClient
from rss2discord.transports.neksio_price_monitor import (
    NeksioPriceMonitor,
    NeksioPriceMonitorDependencies,
)
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


def test_build_price_jobs_includes_enabled_provider_feeds_in_configured_order(
    tmp_path: Path,
) -> None:
    # Given
    clock = FakeClock(maximum_sleeps=1)
    constructed_feed_ids: list[tuple[str, str]] = []
    constructed_anhoch_dependencies: list[AnhochPriceMonitorDependencies] = []
    constructed_neksio_dependencies: list[NeksioPriceMonitorDependencies] = []
    config = AppConfig(
        feeds=(
            FeedConfig(
                id="ordinary",
                url="https://example.test/feed.xml",
                webhook="https://discord.example.test/ordinary",
            ),
            make_anhoch_feed("first", 5),
            make_neksio_feed("second", 7),
            make_anhoch_feed("disabled-anhoch", None),
            make_neksio_feed("third", 11),
            make_neksio_feed("disabled-neksio", None),
        ),
    )

    def anhoch_monitor_factory(
        feed: FeedConfig,
        dependencies: AnhochPriceMonitorDependencies,
    ) -> RecordingMonitor:
        constructed_anhoch_dependencies.append(dependencies)
        constructed_feed_ids.append(("anhoch", feed.id))
        return RecordingMonitor(feed.id, [], clock)

    def neksio_monitor_factory(
        feed: FeedConfig,
        dependencies: NeksioPriceMonitorDependencies,
    ) -> RecordingMonitor:
        constructed_neksio_dependencies.append(dependencies)
        constructed_feed_ids.append(("neksio", feed.id))
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
        )

    # Then
    assert constructed_feed_ids == [
        ("anhoch", "first"),
        ("neksio", "second"),
        ("neksio", "third"),
    ]
    assert [job.interval for job in jobs] == [5, 7, 11]
    assert isinstance(
        constructed_anhoch_dependencies[0].catalog,
        AnhochCatalogClient,
    )
    assert isinstance(
        constructed_neksio_dependencies[0].catalog,
        NeksioCatalogClient,
    )
    assert all(
        dependency.delivery.is_shutdown_requested()
        for dependency in constructed_anhoch_dependencies
    )
    assert all(
        dependency.delivery.is_shutdown_requested()
        for dependency in constructed_neksio_dependencies
    )


def test_build_price_jobs_defaults_select_exact_provider_monitors(
    tmp_path: Path,
) -> None:
    # Given
    config = AppConfig(
        feeds=(
            make_anhoch_feed("anhoch", 5),
            make_neksio_feed("neksio", 7),
        ),
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        dependencies = PriceJobDependencies(
            store=store,
            sender=FakeSender([]),
            sleep=lambda _seconds: True,
            delay_between_posts=0,
            is_shutdown_requested=lambda: False,
        )

        # When
        jobs = build_price_jobs(config, dependencies)

    # Then
    assert [job.interval for job in jobs] == [5, 7]
    assert isinstance(jobs[0].run, partial)
    assert isinstance(jobs[1].run, partial)
    anhoch_monitor = jobs[0].run.args[0]
    neksio_monitor = jobs[1].run.args[0]
    assert type(anhoch_monitor) is AnhochPriceMonitor
    assert type(anhoch_monitor._dependencies) is AnhochPriceMonitorDependencies
    assert type(anhoch_monitor._dependencies.catalog) is AnhochCatalogClient
    assert type(neksio_monitor) is NeksioPriceMonitor
    assert type(neksio_monitor._dependencies) is NeksioPriceMonitorDependencies
    assert type(neksio_monitor._dependencies.catalog) is NeksioCatalogClient


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
