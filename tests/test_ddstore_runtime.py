from pathlib import Path

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.ddstore import DDStoreStrategy
from rss2discord.transports.ddstore_price_monitor import (
    DDStorePriceMonitorDependencies,
)
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock, RecordingMonitor


def make_ddstore_feed(feed_id: str, interval: float | None) -> FeedConfig:
    return FeedConfig(
        id=feed_id,
        url="https://ddstore.mk/",
        webhook=f"https://discord.example.test/webhooks/{feed_id}",
        strategy="ddstore",
        price_check_interval=interval,
    )


def test_app_registers_ddstore_strategy(tmp_path: Path) -> None:
    # Given / When
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(config=AppConfig(), store=store, sender=FakeSender([]))

    # Then
    assert isinstance(app._strategies["ddstore"], DDStoreStrategy)


def test_app_injects_shutdown_callback_into_ddstore_strategy(tmp_path: Path) -> None:
    # Given
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(config=AppConfig(), store=store, sender=FakeSender([]))
        strategy = app._strategies["ddstore"]
        app.request_shutdown()

        # When / Then
        with pytest.raises(FeedFetchInterruptedError):
            strategy.fetch_entries("https://ddstore.mk/")


def test_build_price_jobs_dispatches_only_enabled_ddstore_feeds(
    tmp_path: Path,
) -> None:
    # Given
    clock = FakeClock(maximum_sleeps=1)
    constructed_feed_ids: list[str] = []
    constructed_dependencies: list[DDStorePriceMonitorDependencies] = []
    config = AppConfig(
        feeds=(
            make_ddstore_feed("ddstore", 11),
            make_ddstore_feed("disabled-ddstore", None),
        ),
    )

    def ddstore_monitor_factory(
        feed: FeedConfig,
        dependencies: DDStorePriceMonitorDependencies,
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
            ddstore_monitor_factory=ddstore_monitor_factory,
        )

    # Then
    assert constructed_feed_ids == ["ddstore"]
    assert [job.interval for job in jobs] == [11]
    assert len(constructed_dependencies) == 1
    assert all(
        dependency.delivery.is_shutdown_requested()
        for dependency in constructed_dependencies
    )
