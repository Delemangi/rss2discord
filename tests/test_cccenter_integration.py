from collections.abc import Callable
from pathlib import Path

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig, load_config
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.source_labels import SOURCE_LABEL_CCCENTER, source_label
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.transports.cccenter import CCCenterStrategy
from rss2discord.transports.cccenter_price_monitor import (
    CCCenterPriceMonitorDependencies,
)
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock, RecordingMonitor


def test_cccenter_is_a_configured_strategy_with_source_label() -> None:
    feed = FeedConfig(
        id="cccenter",
        url="https://cccenter.mk/shop/?orderby=date",
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="cccenter",
        price_check_interval=3600,
    )
    assert source_label(feed) == SOURCE_LABEL_CCCENTER


def test_cccenter_strategy_is_registered_in_app(tmp_path: Path) -> None:
    feed = FeedConfig(
        id="cccenter",
        url="https://cccenter.mk/shop/?orderby=date",
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="cccenter",
    )
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(feeds=(feed,)), store, lambda *_: None)  # type: ignore[arg-type]
    assert "cccenter" in app._strategies


def test_config_example_documents_cccenter() -> None:
    config = load_config(Path(__file__).parent.parent / "config" / "config.example.yaml")
    feed = next(feed for feed in config.feeds if feed.id == "cccenter-products")
    assert feed.strategy == "cccenter"
    assert feed.price_check_interval == 3600


def test_cccenter_strategy_passes_app_shutdown_callback_to_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def shutdown() -> bool:
        return True

    strategy = CCCenterStrategy(shutdown)
    observed: list[Callable[[], bool]] = []

    def fetch(
        url: str,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[object, ...]:
        del url
        observed.append(is_shutdown_requested)
        return ()

    monkeypatch.setattr(strategy._client, "fetch_latest_products", fetch)
    strategy.fetch_entries("https://cccenter.mk/shop/?orderby=date")

    assert observed == [shutdown]


def test_cccenter_price_monitor_is_wired_into_runtime_factory(tmp_path: Path) -> None:
    feed = FeedConfig(
        id="cccenter",
        url="https://cccenter.mk/shop/?orderby=date",
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="cccenter",
        price_check_interval=3600,
    )
    constructed: list[tuple[FeedConfig, CCCenterPriceMonitorDependencies]] = []
    clock = FakeClock(maximum_sleeps=1)

    def factory(
        feed: FeedConfig,
        dependencies: CCCenterPriceMonitorDependencies,
    ) -> RecordingMonitor:
        constructed.append((feed, dependencies))
        return RecordingMonitor(feed.id, [], clock)

    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            AppConfig(feeds=(feed,)),
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=lambda _: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
            cccenter_monitor_factory=factory,
        )

    assert [job.interval for job in jobs] == [3600]
    assert constructed[0][0].strategy == "cccenter"
