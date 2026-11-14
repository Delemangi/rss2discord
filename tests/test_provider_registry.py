"""Guard the single provider registry against drift from configuration."""

from functools import partial
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from rss2discord.configuration import AppConfig, FeedConfig, FeedStrategyName
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.source_labels import source_label
from rss2discord.providers.anhoch.prices import AnhochPriceMonitor
from rss2discord.providers.anhoch.strategy import AnhochStrategy
from rss2discord.providers.cccenter.prices import CCCenterPriceMonitor
from rss2discord.providers.cccenter.strategy import CCCenterStrategy
from rss2discord.providers.ddstore.prices import DDStorePriceMonitor
from rss2discord.providers.ddstore.strategy import DDStoreStrategy
from rss2discord.providers.gjirafa50.background_monitor import (
    Gjirafa50BackgroundPriceMonitor,
)
from rss2discord.providers.gjirafa50.strategy import Gjirafa50Strategy
from rss2discord.providers.hivetec.prices import HivetecPriceMonitor
from rss2discord.providers.hivetec.strategy import HivetecStrategy
from rss2discord.providers.itmk_oglasnik.strategy import ITMkOglasnikStrategy
from rss2discord.providers.names import (
    PRICE_STRATEGIES,
    STRATEGY_LABELS,
    STRATEGY_NAMES,
)
from rss2discord.providers.neksio.prices import NeksioPriceMonitor
from rss2discord.providers.neksio.strategy import NeksioStrategy
from rss2discord.providers.neptun.prices import NeptunPriceMonitor
from rss2discord.providers.neptun.strategy import NeptunStrategy
from rss2discord.providers.pazar3.catalog import Pazar3CatalogClient
from rss2discord.providers.pazar3.pacing import Pazar3RequestPacer
from rss2discord.providers.pazar3.prices import Pazar3PriceMonitor
from rss2discord.providers.pazar3.strategy import Pazar3Strategy
from rss2discord.providers.registry import (
    SharedPriceMonitorDependencies,
    build_discovery_strategies,
    build_provider_price_monitor,
)
from rss2discord.providers.reklama5.prices import Reklama5PriceMonitor
from rss2discord.providers.reklama5.strategy import Reklama5Strategy
from rss2discord.providers.setec.prices import SetecPriceMonitor
from rss2discord.providers.setec.strategy import SetecStrategy
from rss2discord.providers.technomarket.prices import TechnomarketPriceMonitor
from rss2discord.providers.technomarket.strategy import TechnomarketStrategy
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.runtime.prices import PriceJobDependencies, build_price_jobs
from rss2discord.transports import RSSStrategy, XenForoStrategy
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock

DEFAULT_PRICE_INTERVAL = 3_600
PRICE_INTERVAL_FOR: dict[str, int] = {"gjirafa50": 21_600}
EXPECTED_DISCOVERY_TYPES: dict[str, type[object]] = {
    "anhoch": AnhochStrategy,
    "cccenter": CCCenterStrategy,
    "ddstore": DDStoreStrategy,
    "gjirafa50": Gjirafa50Strategy,
    "hivetec": HivetecStrategy,
    "itmk_oglasnik": ITMkOglasnikStrategy,
    "neksio": NeksioStrategy,
    "neptun": NeptunStrategy,
    "pazar3": Pazar3Strategy,
    "reklama5": Reklama5Strategy,
    "rss": RSSStrategy,
    "setec": SetecStrategy,
    "technomarket": TechnomarketStrategy,
    "xenforo": XenForoStrategy,
}
EXPECTED_MONITOR_TYPES: dict[str, type[object]] = {
    "anhoch": AnhochPriceMonitor,
    "cccenter": CCCenterPriceMonitor,
    "ddstore": DDStorePriceMonitor,
    "gjirafa50": Gjirafa50BackgroundPriceMonitor,
    "hivetec": HivetecPriceMonitor,
    "neksio": NeksioPriceMonitor,
    "neptun": NeptunPriceMonitor,
    "pazar3": Pazar3PriceMonitor,
    "reklama5": Reklama5PriceMonitor,
    "setec": SetecPriceMonitor,
    "technomarket": TechnomarketPriceMonitor,
}


def make_feed(
    strategy: FeedStrategyName,
    *,
    feed_id: str | None = None,
    interval: float | None = None,
) -> FeedConfig:
    return FeedConfig(
        id=feed_id or f"{strategy}-feed",
        url="https://example.test/feed.xml",
        webhook="https://discord.test/webhooks/id/token",
        strategy=strategy,
        price_check_interval=interval,
    )


def make_shared_dependencies(
    store: DeliveryStore,
    clock: FakeClock,
) -> SharedPriceMonitorDependencies:
    return SharedPriceMonitorDependencies(
        snapshots=store,
        sender=FakeSender([]),
        fetch_retry_policy=FetchRetryPolicy(
            sleep=clock.sleep,
            on_retry=lambda error, delay: None,
        ),
        sqlite_retry_policy=SQLiteRetryPolicy(
            sleep=clock.sleep,
            on_retry=lambda error, delay: None,
        ),
        delivery=PriceAlertDelivery(clock.sleep, 0, lambda: False),
        pazar3_pacer=Pazar3RequestPacer(clock.monotonic),
    )


def test_strategy_names_match_configuration_literal() -> None:
    assert set(get_args(FeedStrategyName)) == set(STRATEGY_NAMES)
    assert set(STRATEGY_LABELS) == set(STRATEGY_NAMES)


def test_build_discovery_strategies_covers_every_strategy() -> None:
    clock = FakeClock(maximum_sleeps=1)
    pacer = Pazar3RequestPacer(clock.monotonic)

    strategies = build_discovery_strategies(
        is_shutdown_requested=lambda: False,
        sleep=clock.sleep,
        pazar3_pacer=pacer,
    )

    assert {name: type(strategy) for name, strategy in strategies.items()} == (
        EXPECTED_DISCOVERY_TYPES
    )
    pazar3 = strategies["pazar3"]
    assert isinstance(pazar3, Pazar3Strategy)
    assert pazar3._pacer is pacer


@pytest.mark.parametrize("strategy", STRATEGY_NAMES)
def test_source_label_uses_registry_label(strategy: FeedStrategyName) -> None:
    assert source_label(make_feed(strategy)) == STRATEGY_LABELS[strategy]


@pytest.mark.parametrize("strategy", sorted(PRICE_STRATEGIES))
def test_price_strategy_builds_monitor(
    strategy: FeedStrategyName,
    tmp_path: Path,
) -> None:
    interval = PRICE_INTERVAL_FOR.get(strategy, DEFAULT_PRICE_INTERVAL)
    feed = make_feed(strategy, interval=interval)
    clock = FakeClock(maximum_sleeps=1)

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = build_provider_price_monitor(
            feed,
            make_shared_dependencies(store, clock),
        )

    assert monitor is not None
    assert type(monitor) is EXPECTED_MONITOR_TYPES[strategy]


@pytest.mark.parametrize("strategy", sorted(set(STRATEGY_NAMES) - PRICE_STRATEGIES))
def test_non_price_strategy_rejects_price_interval(
    strategy: FeedStrategyName,
) -> None:
    with pytest.raises(ValidationError):
        make_feed(strategy, interval=DEFAULT_PRICE_INTERVAL)


def test_gjirafa50_requires_long_price_interval() -> None:
    with pytest.raises(ValidationError):
        make_feed("gjirafa50", interval=DEFAULT_PRICE_INTERVAL)

    assert (
        make_feed(
            "gjirafa50",
            interval=PRICE_INTERVAL_FOR["gjirafa50"],
        ).price_check_interval
        == 21_600
    )


def test_build_price_jobs_preserves_feed_order(tmp_path: Path) -> None:
    clock = FakeClock(maximum_sleeps=1)
    config = AppConfig(
        feeds=(
            make_feed("anhoch", interval=3_600),
            make_feed("neksio", interval=5_400),
            make_feed("pazar3", interval=7_200),
            make_feed("anhoch", feed_id="anhoch-disabled", interval=None),
        ),
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            config,
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=clock.sleep,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
        )

    assert [job.name for job in jobs] == ["anhoch-feed", "neksio-feed", "pazar3-feed"]
    assert [job.interval for job in jobs] == [3_600, 5_400, 7_200]


def test_build_price_jobs_injects_shared_pazar3_pacer(tmp_path: Path) -> None:
    clock = FakeClock(maximum_sleeps=1)
    pacer = Pazar3RequestPacer(clock.monotonic)
    config = AppConfig(feeds=(make_feed("pazar3", interval=3_600),))

    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            config,
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=clock.sleep,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
                pazar3_pacer=pacer,
            ),
        )

    assert len(jobs) == 1
    run = jobs[0].run
    assert isinstance(run, partial)
    monitor = run.args[0]
    assert type(monitor) is Pazar3PriceMonitor
    assert isinstance(monitor._dependencies.catalog, Pazar3CatalogClient)
    assert monitor._dependencies.catalog._pacer is pacer


def test_build_price_jobs_propagates_shutdown_to_monitors(tmp_path: Path) -> None:
    clock = FakeClock(maximum_sleeps=1)
    config = AppConfig(
        feeds=(
            make_feed("anhoch", interval=3_600),
            make_feed("pazar3", interval=3_600),
        ),
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            config,
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=clock.sleep,
                delay_between_posts=0,
                is_shutdown_requested=lambda: True,
            ),
        )

    assert len(jobs) == 2
    for job in jobs:
        run = job.run
        assert isinstance(run, partial)
        monitor = run.args[0]
        assert monitor._dependencies.delivery.is_shutdown_requested() is True
