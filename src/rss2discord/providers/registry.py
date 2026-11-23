"""Single registry composing discovery strategies and price monitors per provider."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import SleepCallback
from rss2discord.providers.air_quality.strategy import AirQualityStrategy
from rss2discord.providers.ananas.catalog import AnanasCatalogClient
from rss2discord.providers.ananas.prices import AnanasPriceMonitor
from rss2discord.providers.ananas.strategy import AnanasStrategy
from rss2discord.providers.anhoch.catalog import AnhochCatalogClient
from rss2discord.providers.anhoch.prices import AnhochPriceMonitor
from rss2discord.providers.anhoch.strategy import AnhochStrategy
from rss2discord.providers.arctic_shift.client import ArcticShiftClient
from rss2discord.providers.background import (
    BackgroundPriceMonitor,
    PriceMonitor,
    PriceMonitorFactory,
    SharedPriceMonitorDependencies,
)
from rss2discord.providers.cccenter.catalog import CCCenterCatalogClient
from rss2discord.providers.cccenter.prices import CCCenterPriceMonitor
from rss2discord.providers.cccenter.strategy import CCCenterStrategy
from rss2discord.providers.ddstore.catalog import DDStoreCatalogClient
from rss2discord.providers.ddstore.prices import DDStorePriceMonitor
from rss2discord.providers.ddstore.strategy import DDStoreStrategy
from rss2discord.providers.emsc.strategy import EmscStrategy
from rss2discord.providers.fakulteti_mk.strategy import FakultetiMkStrategy
from rss2discord.providers.gjirafa50.background_monitor import (
    Gjirafa50BackgroundPriceMonitor,
)
from rss2discord.providers.gjirafa50.catalog import Gjirafa50CatalogClient
from rss2discord.providers.gjirafa50.prices import Gjirafa50PriceMonitorDependencies
from rss2discord.providers.gjirafa50.strategy import Gjirafa50Strategy
from rss2discord.providers.hetzner_auction.strategy import HetznerAuctionStrategy
from rss2discord.providers.hetzner_cloud.client import (
    HetznerCloudCatalogClient,
)
from rss2discord.providers.hetzner_cloud.prices import HetznerCloudPriceMonitor
from rss2discord.providers.hivetec.catalog import HivetecCatalogClient
from rss2discord.providers.hivetec.prices import HivetecPriceMonitor
from rss2discord.providers.hivetec.strategy import HivetecStrategy
from rss2discord.providers.itmk_oglasnik.strategy import ITMkOglasnikStrategy
from rss2discord.providers.neksio.catalog import NeksioCatalogClient
from rss2discord.providers.neksio.prices import NeksioPriceMonitor
from rss2discord.providers.neksio.strategy import NeksioStrategy
from rss2discord.providers.neptun.catalog import NeptunCatalogClient
from rss2discord.providers.neptun.prices import NeptunPriceMonitor
from rss2discord.providers.neptun.strategy import NeptunStrategy
from rss2discord.providers.openrouter_models.client import OpenRouterModelsClient
from rss2discord.providers.openrouter_models.prices import (
    OpenRouterPriceMonitor,
)
from rss2discord.providers.openrouter_models.strategy import (
    OpenRouterModelsStrategy,
)
from rss2discord.providers.pazar3.catalog import Pazar3CatalogClient
from rss2discord.providers.pazar3.pacing import Pazar3RequestPacer
from rss2discord.providers.pazar3.prices import Pazar3PriceMonitor
from rss2discord.providers.pazar3.strategy import Pazar3Strategy
from rss2discord.providers.proxnet.catalog import ProxnetCatalogClient
from rss2discord.providers.proxnet.prices import ProxnetPriceMonitor
from rss2discord.providers.proxnet.strategy import ProxnetStrategy
from rss2discord.providers.reddit.strategy import RedditStrategy
from rss2discord.providers.reklama5.catalog import Reklama5CatalogClient
from rss2discord.providers.reklama5.prices import Reklama5PriceMonitor
from rss2discord.providers.reklama5.strategy import Reklama5Strategy
from rss2discord.providers.setec.catalog import SetecCatalogClient
from rss2discord.providers.setec.prices import SetecPriceMonitor
from rss2discord.providers.setec.strategy import SetecStrategy
from rss2discord.providers.technomarket.catalog import TechnomarketCatalogClient
from rss2discord.providers.technomarket.prices import TechnomarketPriceMonitor
from rss2discord.providers.technomarket.strategy import TechnomarketStrategy
from rss2discord.providers.time_mk.strategy import TimeMkStrategy
from rss2discord.providers.tweets.strategy import TweetsStrategy
from rss2discord.providers.wolfinet.catalog import WolfinetCatalogClient
from rss2discord.providers.wolfinet.prices import WolfinetPriceMonitor
from rss2discord.providers.wolfinet.strategy import WolfinetStrategy
from rss2discord.providers.woocommerce.catalog import WooCommerceCatalogClient
from rss2discord.providers.woocommerce.prices import WooCommercePriceMonitor
from rss2discord.providers.woocommerce.strategy import WooCommerceStrategy
from rss2discord.transports import RSSStrategy, ScraperStrategy, XenForoStrategy
from rss2discord.transports.price_monitor import (
    PriceMonitorDependencies,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoveryDependencies:
    """Collaborators shared by discovery strategy construction."""

    is_shutdown_requested: Callable[[], bool]
    sleep: SleepCallback
    pazar3_pacer: Pazar3RequestPacer


DiscoveryFactory = Callable[[DiscoveryDependencies], ScraperStrategy]


def _monitor_dependencies[CatalogT](
    catalog: CatalogT,
    dependencies: SharedPriceMonitorDependencies,
) -> PriceMonitorDependencies[CatalogT]:
    return PriceMonitorDependencies(
        catalog=catalog,
        snapshots=dependencies.snapshots,
        sender=dependencies.sender,
        fetch_retry_policy=dependencies.fetch_retry_policy,
        sqlite_retry_policy=dependencies.sqlite_retry_policy,
        delivery=dependencies.delivery,
    )


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """Construction entry for one feed strategy."""

    discovery: DiscoveryFactory | None = None
    price_monitor: PriceMonitorFactory | None = None


_STRATEGY_SPECS: Final[dict[str, StrategySpec]] = {
    "air_quality": StrategySpec(
        discovery=lambda dependencies: AirQualityStrategy(),
    ),
    "ananas": StrategySpec(
        discovery=lambda dependencies: AnanasStrategy(
            dependencies.is_shutdown_requested,
        ),
        price_monitor=lambda feed, dependencies: AnanasPriceMonitor(
            feed,
            _monitor_dependencies(AnanasCatalogClient(), dependencies),
        ),
    ),
    "anhoch": StrategySpec(
        discovery=lambda dependencies: AnhochStrategy(),
        price_monitor=lambda feed, dependencies: AnhochPriceMonitor(
            feed,
            _monitor_dependencies(AnhochCatalogClient(), dependencies),
        ),
    ),
    "cccenter": StrategySpec(
        discovery=lambda dependencies: CCCenterStrategy(
            dependencies.is_shutdown_requested,
        ),
        price_monitor=lambda feed, dependencies: CCCenterPriceMonitor(
            feed,
            _monitor_dependencies(CCCenterCatalogClient(), dependencies),
        ),
    ),
    "ddstore": StrategySpec(
        discovery=lambda dependencies: DDStoreStrategy(
            dependencies.is_shutdown_requested,
        ),
        price_monitor=lambda feed, dependencies: DDStorePriceMonitor(
            feed,
            _monitor_dependencies(DDStoreCatalogClient(), dependencies),
        ),
    ),
    "emsc": StrategySpec(discovery=lambda dependencies: EmscStrategy()),
    "fakulteti_mk": StrategySpec(
        discovery=lambda dependencies: FakultetiMkStrategy(),
    ),
    "gjirafa50": StrategySpec(
        discovery=lambda dependencies: Gjirafa50Strategy(
            dependencies.is_shutdown_requested,
        ),
        price_monitor=lambda feed, dependencies: Gjirafa50BackgroundPriceMonitor(
            feed,
            Gjirafa50PriceMonitorDependencies(
                catalog=Gjirafa50CatalogClient(),
                snapshots=dependencies.snapshots,
                sender=dependencies.sender,
                fetch_retry_policy=dependencies.fetch_retry_policy,
                sqlite_retry_policy=dependencies.sqlite_retry_policy,
                delivery=dependencies.delivery,
                database_path=dependencies.database_path,
                cooldown_peer_feed_ids=dependencies.cooldown_peer_feed_ids,
            ),
        ),
    ),
    "hetzner_auction": StrategySpec(
        discovery=lambda dependencies: HetznerAuctionStrategy(),
    ),
    "hetzner_cloud": StrategySpec(
        price_monitor=lambda feed, dependencies: HetznerCloudPriceMonitor(
            feed,
            _monitor_dependencies(HetznerCloudCatalogClient(), dependencies),
        ),
    ),
    "hivetec": StrategySpec(
        discovery=lambda dependencies: HivetecStrategy(
            dependencies.is_shutdown_requested,
        ),
        price_monitor=lambda feed, dependencies: BackgroundPriceMonitor(
            feed,
            dependencies,
            lambda worker_feed, worker_dependencies: HivetecPriceMonitor(
                worker_feed,
                _monitor_dependencies(
                    HivetecCatalogClient(),
                    worker_dependencies,
                ),
            ),
        ),
    ),
    "itmk_oglasnik": StrategySpec(
        discovery=lambda dependencies: ITMkOglasnikStrategy(),
    ),
    "neksio": StrategySpec(
        discovery=lambda dependencies: NeksioStrategy(
            dependencies.is_shutdown_requested,
        ),
        price_monitor=lambda feed, dependencies: BackgroundPriceMonitor(
            feed,
            dependencies,
            lambda worker_feed, worker_dependencies: NeksioPriceMonitor(
                worker_feed,
                _monitor_dependencies(
                    NeksioCatalogClient(),
                    worker_dependencies,
                ),
            ),
        ),
    ),
    "neptun": StrategySpec(
        discovery=lambda dependencies: NeptunStrategy(),
        price_monitor=lambda feed, dependencies: NeptunPriceMonitor(
            feed,
            _monitor_dependencies(NeptunCatalogClient(), dependencies),
        ),
    ),
    "openrouter_models": StrategySpec(
        discovery=lambda dependencies: OpenRouterModelsStrategy(),
    ),
    "openrouter_prices": StrategySpec(
        price_monitor=lambda feed, dependencies: OpenRouterPriceMonitor(
            feed,
            _monitor_dependencies(OpenRouterModelsClient(), dependencies),
        ),
    ),
    "pazar3": StrategySpec(
        discovery=lambda dependencies: Pazar3Strategy(
            pacer=dependencies.pazar3_pacer,
            sleep=dependencies.sleep,
            is_shutdown_requested=dependencies.is_shutdown_requested,
        ),
        price_monitor=lambda feed, dependencies: Pazar3PriceMonitor(
            feed,
            _monitor_dependencies(
                Pazar3CatalogClient(
                    dependencies.pazar3_pacer,
                    dependencies.delivery.sleep,
                ),
                dependencies,
            ),
        ),
    ),
    "reddit": StrategySpec(discovery=lambda dependencies: RedditStrategy()),
    "arctic_shift": StrategySpec(
        discovery=lambda dependencies: RedditStrategy(
            client=ArcticShiftClient(
                is_shutdown_requested=dependencies.is_shutdown_requested,
            ),
            show_engagement=False,
            filter_removed=True,
        ),
    ),
    "arctic_shift_nsfw": StrategySpec(
        discovery=lambda dependencies: RedditStrategy(
            client=ArcticShiftClient(
                is_shutdown_requested=dependencies.is_shutdown_requested,
            ),
            show_engagement=False,
            filter_removed=True,
            allow_nsfw=True,
        ),
    ),
    "proxnet": StrategySpec(
        discovery=lambda dependencies: ProxnetStrategy(
            dependencies.is_shutdown_requested,
        ),
        price_monitor=lambda feed, dependencies: ProxnetPriceMonitor(
            feed,
            _monitor_dependencies(ProxnetCatalogClient(), dependencies),
        ),
    ),
    "reklama5": StrategySpec(
        discovery=lambda dependencies: Reklama5Strategy(
            is_shutdown_requested=dependencies.is_shutdown_requested,
            sleep=dependencies.sleep,
        ),
        price_monitor=lambda feed, dependencies: BackgroundPriceMonitor(
            feed,
            dependencies,
            lambda worker_feed, worker_dependencies: Reklama5PriceMonitor(
                worker_feed,
                _monitor_dependencies(
                    Reklama5CatalogClient(
                        sleep=worker_dependencies.delivery.sleep,
                    ),
                    worker_dependencies,
                ),
            ),
        ),
    ),
    "rss": StrategySpec(discovery=lambda dependencies: RSSStrategy()),
    "setec": StrategySpec(
        discovery=lambda dependencies: SetecStrategy(),
        price_monitor=lambda feed, dependencies: SetecPriceMonitor(
            feed,
            _monitor_dependencies(SetecCatalogClient(), dependencies),
        ),
    ),
    "technomarket": StrategySpec(
        discovery=lambda dependencies: TechnomarketStrategy(
            dependencies.is_shutdown_requested,
        ),
        price_monitor=lambda feed, dependencies: TechnomarketPriceMonitor(
            feed,
            _monitor_dependencies(TechnomarketCatalogClient(), dependencies),
        ),
    ),
    "time_mk": StrategySpec(discovery=lambda dependencies: TimeMkStrategy()),
    "tweets": StrategySpec(discovery=lambda dependencies: TweetsStrategy()),
    "tweets_retweets": StrategySpec(
        discovery=lambda dependencies: TweetsStrategy(include_retweets=True),
    ),
    "tweets_all": StrategySpec(
        discovery=lambda dependencies: TweetsStrategy(
            include_retweets=True,
            include_replies=True,
            include_pinned=True,
        ),
    ),
    "wolfinet": StrategySpec(
        discovery=lambda dependencies: WolfinetStrategy(
            dependencies.is_shutdown_requested,
        ),
        price_monitor=lambda feed, dependencies: WolfinetPriceMonitor(
            feed,
            _monitor_dependencies(WolfinetCatalogClient(), dependencies),
        ),
    ),
    "woocommerce": StrategySpec(
        discovery=lambda dependencies: WooCommerceStrategy(
            dependencies.is_shutdown_requested,
        ),
        price_monitor=lambda feed, dependencies: WooCommercePriceMonitor(
            feed,
            _monitor_dependencies(WooCommerceCatalogClient(), dependencies),
        ),
    ),
    "xenforo": StrategySpec(discovery=lambda dependencies: XenForoStrategy()),
}
STRATEGY_SPECS: Final[Mapping[str, StrategySpec]] = MappingProxyType(_STRATEGY_SPECS)


def has_discovery_strategy(strategy: str) -> bool:
    """Return whether a configured strategy produces ordinary feed entries."""
    spec = STRATEGY_SPECS.get(strategy)
    return spec is not None and spec.discovery is not None


def build_discovery_strategies(
    *,
    is_shutdown_requested: Callable[[], bool],
    sleep: SleepCallback,
    pazar3_pacer: Pazar3RequestPacer,
) -> dict[str, ScraperStrategy]:
    dependencies = DiscoveryDependencies(
        is_shutdown_requested=is_shutdown_requested,
        sleep=sleep,
        pazar3_pacer=pazar3_pacer,
    )
    strategies: dict[str, ScraperStrategy] = {}
    for name, spec in STRATEGY_SPECS.items():
        if spec.discovery is None:
            continue
        strategies[name] = spec.discovery(dependencies)
    return strategies


def build_provider_price_monitor(
    feed: FeedConfig,
    dependencies: SharedPriceMonitorDependencies,
) -> PriceMonitor | None:
    try:
        spec = STRATEGY_SPECS[feed.strategy]
    except KeyError:
        raise RuntimeError(
            f"no strategy spec registered for {feed.strategy!r}",
        ) from None
    if spec.price_monitor is None:
        return None
    return spec.price_monitor(feed, dependencies)
