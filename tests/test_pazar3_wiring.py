from functools import partial
from pathlib import Path

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.source_labels import source_label
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.transports.pazar3 import Pazar3Strategy
from rss2discord.transports.pazar3_catalog import Pazar3CatalogClient
from rss2discord.transports.pazar3_price_monitor import (
    Pazar3PriceMonitor,
    Pazar3PriceMonitorDependencies,
)
from tests.app_helpers import FakeSender


def make_feed(*, interval: float | None = None) -> FeedConfig:
    return FeedConfig(
        id="pazar3-computer-parts",
        url=(
            "https://www.pazar3.mk/oglasi/elektronika/"
            "delovi-za-kompjuteri-dodatoci/prodazba"
        ),
        webhook="https://discord.test/webhooks/id/token",
        strategy="pazar3",
        price_check_interval=interval,
    )


def test_pazar3_configuration_accepts_optional_positive_price_interval() -> None:
    assert make_feed().price_check_interval is None
    assert make_feed(interval=3600).price_check_interval == 3600


def test_app_registers_pazar3_strategy(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(), store, FakeSender([]))

    assert "pazar3" in app._strategies
    strategy = app._strategies["pazar3"]
    assert isinstance(strategy, Pazar3Strategy)
    assert strategy._pacer is app._pazar3_pacer


def test_source_label_returns_pazar3() -> None:
    assert source_label(make_feed()) == "Pazar3"


def test_price_runtime_injects_shared_pazar3_pacer(tmp_path: Path) -> None:
    config = AppConfig(feeds=(make_feed(interval=3600),))
    constructed: list[Pazar3PriceMonitorDependencies] = []

    def factory(
        feed: FeedConfig,
        dependencies: Pazar3PriceMonitorDependencies,
    ) -> Pazar3PriceMonitor:
        constructed.append(dependencies)
        return Pazar3PriceMonitor(feed, dependencies)

    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(config, store, FakeSender([]))
        jobs = build_price_jobs(
            config,
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=lambda seconds: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
                pazar3_pacer=app._pazar3_pacer,
            ),
            pazar3_monitor_factory=factory,
        )

    job = jobs[0].run
    assert isinstance(job, partial)
    price_monitor = job.args[0]
    assert type(price_monitor) is Pazar3PriceMonitor
    assert type(price_monitor._dependencies) is Pazar3PriceMonitorDependencies
    assert type(price_monitor._dependencies.catalog) is Pazar3CatalogClient
    assert price_monitor._dependencies.catalog._pacer is app._pazar3_pacer
    assert constructed == [price_monitor._dependencies]
