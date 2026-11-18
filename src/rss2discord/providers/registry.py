"""Single registry composing discovery strategies and price monitors per provider."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, assert_never

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordSender, SleepCallback
from rss2discord.providers.ananas.catalog import AnanasCatalogClient
from rss2discord.providers.ananas.prices import AnanasCatalog, AnanasPriceMonitor
from rss2discord.providers.ananas.strategy import AnanasStrategy
from rss2discord.providers.anhoch.catalog import AnhochCatalogClient
from rss2discord.providers.anhoch.prices import AnhochCatalog, AnhochPriceMonitor
from rss2discord.providers.anhoch.strategy import AnhochStrategy
from rss2discord.providers.cccenter.catalog import CCCenterCatalogClient
from rss2discord.providers.cccenter.prices import (
    CCCenterCatalog,
    CCCenterPriceMonitor,
)
from rss2discord.providers.cccenter.strategy import CCCenterStrategy
from rss2discord.providers.ddstore.catalog import DDStoreCatalogClient
from rss2discord.providers.ddstore.prices import DDStoreCatalog, DDStorePriceMonitor
from rss2discord.providers.ddstore.strategy import DDStoreStrategy
from rss2discord.providers.fakulteti_mk.strategy import FakultetiMkStrategy
from rss2discord.providers.gjirafa50.background_monitor import (
    Gjirafa50BackgroundPriceMonitor,
)
from rss2discord.providers.gjirafa50.catalog import Gjirafa50CatalogClient
from rss2discord.providers.gjirafa50.prices import Gjirafa50PriceMonitorDependencies
from rss2discord.providers.gjirafa50.strategy import Gjirafa50Strategy
from rss2discord.providers.hivetec.catalog import HivetecCatalogClient
from rss2discord.providers.hivetec.prices import HivetecCatalog, HivetecPriceMonitor
from rss2discord.providers.hivetec.strategy import HivetecStrategy
from rss2discord.providers.itmk_oglasnik.strategy import ITMkOglasnikStrategy
from rss2discord.providers.neksio.catalog import NeksioCatalogClient
from rss2discord.providers.neksio.prices import NeksioCatalog, NeksioPriceMonitor
from rss2discord.providers.neksio.strategy import NeksioStrategy
from rss2discord.providers.neptun.catalog import NeptunCatalogClient
from rss2discord.providers.neptun.prices import NeptunCatalog, NeptunPriceMonitor
from rss2discord.providers.neptun.strategy import NeptunStrategy
from rss2discord.providers.opencode_tweets.strategy import OpenCodeTweetsStrategy
from rss2discord.providers.pazar3.catalog import Pazar3CatalogClient
from rss2discord.providers.pazar3.pacing import Pazar3RequestPacer
from rss2discord.providers.pazar3.prices import Pazar3Catalog, Pazar3PriceMonitor
from rss2discord.providers.pazar3.strategy import Pazar3Strategy
from rss2discord.providers.reklama5.catalog import Reklama5CatalogClient
from rss2discord.providers.reklama5.prices import (
    Reklama5Catalog,
    Reklama5PriceMonitor,
)
from rss2discord.providers.reklama5.strategy import Reklama5Strategy
from rss2discord.providers.setec.catalog import SetecCatalogClient
from rss2discord.providers.setec.prices import SetecCatalog, SetecPriceMonitor
from rss2discord.providers.setec.strategy import SetecStrategy
from rss2discord.providers.technomarket.catalog import TechnomarketCatalogClient
from rss2discord.providers.technomarket.prices import (
    TechnomarketCatalog,
    TechnomarketPriceMonitor,
)
from rss2discord.providers.technomarket.strategy import TechnomarketStrategy
from rss2discord.providers.time_mk.strategy import TimeMkStrategy
from rss2discord.providers.woocommerce.catalog import WooCommerceCatalogClient
from rss2discord.providers.woocommerce.prices import (
    WooCommerceCatalog,
    WooCommercePriceMonitor,
)
from rss2discord.providers.woocommerce.strategy import WooCommerceStrategy
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports import RSSStrategy, ScraperStrategy, XenForoStrategy
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceMonitorDependencies,
)


class PriceMonitor(Protocol):
    def scan(self) -> None: ...


def build_discovery_strategies(
    *,
    is_shutdown_requested: Callable[[], bool],
    sleep: SleepCallback,
    pazar3_pacer: Pazar3RequestPacer,
) -> dict[str, ScraperStrategy]:
    return {
        "ananas": AnanasStrategy(is_shutdown_requested),
        "anhoch": AnhochStrategy(),
        "cccenter": CCCenterStrategy(is_shutdown_requested),
        "ddstore": DDStoreStrategy(is_shutdown_requested),
        "fakulteti_mk": FakultetiMkStrategy(),
        "gjirafa50": Gjirafa50Strategy(is_shutdown_requested),
        "hivetec": HivetecStrategy(is_shutdown_requested),
        "itmk_oglasnik": ITMkOglasnikStrategy(),
        "neksio": NeksioStrategy(is_shutdown_requested),
        "neptun": NeptunStrategy(),
        "opencode_tweets": OpenCodeTweetsStrategy(),
        "pazar3": Pazar3Strategy(
            pacer=pazar3_pacer,
            sleep=sleep,
            is_shutdown_requested=is_shutdown_requested,
        ),
        "reklama5": Reklama5Strategy(is_shutdown_requested=is_shutdown_requested),
        "rss": RSSStrategy(),
        "setec": SetecStrategy(),
        "technomarket": TechnomarketStrategy(is_shutdown_requested),
        "time_mk": TimeMkStrategy(),
        "woocommerce": WooCommerceStrategy(is_shutdown_requested),
        "xenforo": XenForoStrategy(),
    }


@dataclass(frozen=True, slots=True)
class SharedPriceMonitorDependencies:
    snapshots: DeliveryStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery
    pazar3_pacer: Pazar3RequestPacer
    cooldown_peer_feed_ids: tuple[str, ...] = ()


def build_provider_price_monitor(
    feed: FeedConfig,
    dependencies: SharedPriceMonitorDependencies,
) -> PriceMonitor | None:
    match feed.strategy:
        case "ananas":
            return AnanasPriceMonitor(
                feed,
                PriceMonitorDependencies[AnanasCatalog](
                    catalog=AnanasCatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "anhoch":
            return AnhochPriceMonitor(
                feed,
                PriceMonitorDependencies[AnhochCatalog](
                    catalog=AnhochCatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "cccenter":
            return CCCenterPriceMonitor(
                feed,
                PriceMonitorDependencies[CCCenterCatalog](
                    catalog=CCCenterCatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "ddstore":
            return DDStorePriceMonitor(
                feed,
                PriceMonitorDependencies[DDStoreCatalog](
                    catalog=DDStoreCatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "gjirafa50":
            return Gjirafa50BackgroundPriceMonitor(
                feed,
                Gjirafa50PriceMonitorDependencies(
                    catalog=Gjirafa50CatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                    database_path=dependencies.snapshots.database_path,
                    cooldown_peer_feed_ids=dependencies.cooldown_peer_feed_ids,
                ),
            )
        case "hivetec":
            return HivetecPriceMonitor(
                feed,
                PriceMonitorDependencies[HivetecCatalog](
                    catalog=HivetecCatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "neksio":
            return NeksioPriceMonitor(
                feed,
                PriceMonitorDependencies[NeksioCatalog](
                    catalog=NeksioCatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "neptun":
            return NeptunPriceMonitor(
                feed,
                PriceMonitorDependencies[NeptunCatalog](
                    catalog=NeptunCatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "pazar3":
            return Pazar3PriceMonitor(
                feed,
                PriceMonitorDependencies[Pazar3Catalog](
                    catalog=Pazar3CatalogClient(
                        dependencies.pazar3_pacer,
                        dependencies.delivery.sleep,
                    ),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "reklama5":
            return Reklama5PriceMonitor(
                feed,
                PriceMonitorDependencies[Reklama5Catalog](
                    catalog=Reklama5CatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "setec":
            return SetecPriceMonitor(
                feed,
                PriceMonitorDependencies[SetecCatalog](
                    catalog=SetecCatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "technomarket":
            return TechnomarketPriceMonitor(
                feed,
                PriceMonitorDependencies[TechnomarketCatalog](
                    catalog=TechnomarketCatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "woocommerce":
            return WooCommercePriceMonitor(
                feed,
                PriceMonitorDependencies[WooCommerceCatalog](
                    catalog=WooCommerceCatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case (
            "rss"
            | "xenforo"
            | "itmk_oglasnik"
            | "time_mk"
            | "fakulteti_mk"
            | "opencode_tweets"
        ):
            return None
        case unreachable:
            assert_never(unreachable)
