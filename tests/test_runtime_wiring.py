from pathlib import Path

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_runtime import PriceJobDependencies
from rss2discord.scheduler import ScheduledJob
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock


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
            FeedConfig(
                id="first",
                url="https://catalog.example.test/first",
                webhook="https://discord.example.test/first",
                strategy="anhoch",
                price_check_interval=5,
            ),
            FeedConfig(
                id="second",
                url="https://catalog.example.test/second",
                webhook="https://discord.example.test/second",
                strategy="anhoch",
                price_check_interval=7,
            ),
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
