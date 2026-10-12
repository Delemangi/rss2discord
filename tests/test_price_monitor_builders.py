from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig, FeedStrategyName
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_monitor_builders import (
    SharedPriceMonitorDependencies,
    build_provider_price_monitor,
)
from rss2discord.providers.anhoch.prices import AnhochPriceMonitorDependencies
from rss2discord.providers.cccenter.prices import CCCenterPriceMonitorDependencies
from rss2discord.providers.ddstore.prices import DDStorePriceMonitorDependencies
from rss2discord.providers.gjirafa50.prices import Gjirafa50PriceMonitorDependencies
from rss2discord.providers.hivetec.prices import HivetecPriceMonitorDependencies
from rss2discord.providers.neksio.prices import NeksioPriceMonitorDependencies
from rss2discord.providers.neptun.prices import NeptunPriceMonitorDependencies
from rss2discord.providers.pazar3.catalog import Pazar3CatalogClient
from rss2discord.providers.pazar3.pacing import Pazar3RequestPacer
from rss2discord.providers.pazar3.prices import Pazar3PriceMonitorDependencies
from rss2discord.providers.reklama5.prices import (
    Reklama5PriceMonitorDependencies,
)
from rss2discord.providers.setec.prices import SetecPriceMonitorDependencies
from rss2discord.providers.technomarket.prices import (
    TechnomarketPriceMonitorDependencies,
)
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock


@pytest.mark.parametrize(
    ("strategy", "constructor_name", "dependency_type"),
    [
        ("anhoch", "AnhochPriceMonitor", AnhochPriceMonitorDependencies),
        ("cccenter", "CCCenterPriceMonitor", CCCenterPriceMonitorDependencies),
        ("ddstore", "DDStorePriceMonitor", DDStorePriceMonitorDependencies),
        ("hivetec", "HivetecPriceMonitor", HivetecPriceMonitorDependencies),
        (
            "gjirafa50",
            "Gjirafa50BackgroundPriceMonitor",
            Gjirafa50PriceMonitorDependencies,
        ),
        ("neksio", "NeksioPriceMonitor", NeksioPriceMonitorDependencies),
        ("neptun", "NeptunPriceMonitor", NeptunPriceMonitorDependencies),
        ("pazar3", "Pazar3PriceMonitor", Pazar3PriceMonitorDependencies),
        (
            "reklama5",
            "Reklama5PriceMonitor",
            Reklama5PriceMonitorDependencies,
        ),
        ("setec", "SetecPriceMonitor", SetecPriceMonitorDependencies),
        (
            "technomarket",
            "TechnomarketPriceMonitor",
            TechnomarketPriceMonitorDependencies,
        ),
    ],
)
def test_builder_selects_native_monitor_and_typed_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strategy: FeedStrategyName,
    constructor_name: str,
    dependency_type: type[object],
) -> None:
    feed = FeedConfig(
        id="provider",
        url="https://provider.example.test/catalog",
        webhook="https://discord.example.test/webhook",
        strategy=strategy,
        price_check_interval=21_600 if strategy == "gjirafa50" else 3600,
    )
    clock = FakeClock(maximum_sleeps=1)
    constructed: list[tuple[FeedConfig, object]] = []

    class StubMonitor:
        def scan(self) -> None:
            return

    def construct(built_feed: FeedConfig, dependencies: object) -> StubMonitor:
        constructed.append((built_feed, dependencies))
        return StubMonitor()

    monkeypatch.setattr(
        "rss2discord.price_monitor_builders." + constructor_name,
        construct,
    )
    sleep = clock.sleep
    pacer = Pazar3RequestPacer(clock.monotonic)

    with DeliveryStore(tmp_path / "state.db") as store:
        shared = SharedPriceMonitorDependencies(
            snapshots=store,
            sender=FakeSender([]),
            fetch_retry_policy=FetchRetryPolicy(
                sleep=sleep,
                on_retry=lambda error, delay: None,
            ),
            sqlite_retry_policy=SQLiteRetryPolicy(
                sleep=sleep,
                on_retry=lambda error, delay: None,
            ),
            delivery=PriceAlertDelivery(sleep, 2, lambda: False),
            pazar3_pacer=pacer,
            cooldown_peer_feed_ids=("provider", "peer"),
        )
        monitor = build_provider_price_monitor(feed, shared)

    assert isinstance(monitor, StubMonitor)
    assert len(constructed) == 1
    assert constructed[0][0] is feed
    dependencies = constructed[0][1]
    assert isinstance(dependencies, dependency_type)
    assert isinstance(
        dependencies,
        (
            AnhochPriceMonitorDependencies,
            CCCenterPriceMonitorDependencies,
            DDStorePriceMonitorDependencies,
            HivetecPriceMonitorDependencies,
            Gjirafa50PriceMonitorDependencies,
            NeksioPriceMonitorDependencies,
            NeptunPriceMonitorDependencies,
            Pazar3PriceMonitorDependencies,
            Reklama5PriceMonitorDependencies,
            SetecPriceMonitorDependencies,
            TechnomarketPriceMonitorDependencies,
        ),
    )
    assert dependencies.snapshots is shared.snapshots
    assert dependencies.sender is shared.sender
    assert dependencies.fetch_retry_policy is shared.fetch_retry_policy
    assert dependencies.sqlite_retry_policy is shared.sqlite_retry_policy
    assert dependencies.delivery is shared.delivery
    if isinstance(dependencies, Gjirafa50PriceMonitorDependencies):
        assert dependencies.database_path == store.database_path
        assert dependencies.cooldown_peer_feed_ids == ("provider", "peer")
    if isinstance(dependencies, Pazar3PriceMonitorDependencies):
        assert isinstance(dependencies.catalog, Pazar3CatalogClient)
        assert dependencies.catalog._pacer is pacer
        assert dependencies.catalog._sleep is sleep


@pytest.mark.parametrize("strategy", ["rss", "xenforo", "itmk_oglasnik"])
def test_builder_omits_strategies_without_price_monitors(
    tmp_path: Path,
    strategy: FeedStrategyName,
) -> None:
    feed = FeedConfig(
        id="ordinary",
        url="https://example.test/feed",
        webhook="https://discord.example.test/webhook",
        strategy=strategy,
        price_check_interval=None,
    )
    clock = FakeClock(maximum_sleeps=1)
    sleep = clock.sleep
    with DeliveryStore(tmp_path / "state.db") as store:
        dependencies = SharedPriceMonitorDependencies(
            snapshots=store,
            sender=FakeSender([]),
            fetch_retry_policy=FetchRetryPolicy(
                sleep=sleep,
                on_retry=lambda error, delay: None,
            ),
            sqlite_retry_policy=SQLiteRetryPolicy(
                sleep=sleep,
                on_retry=lambda error, delay: None,
            ),
            delivery=PriceAlertDelivery(sleep, 0, lambda: False),
            pazar3_pacer=Pazar3RequestPacer(clock.monotonic),
        )
        assert build_provider_price_monitor(feed, dependencies) is None
