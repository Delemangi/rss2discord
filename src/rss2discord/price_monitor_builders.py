from dataclasses import dataclass
from typing import Protocol, assert_never

from .configuration import FeedConfig
from .delivery_store import DeliveryStore
from .discord.client import DiscordSender
from .providers.anhoch.catalog import AnhochCatalogClient
from .providers.anhoch.prices import (
    AnhochPriceMonitor,
    AnhochPriceMonitorDependencies,
)
from .providers.cccenter.catalog import CCCenterCatalogClient
from .providers.cccenter.prices import (
    CCCenterPriceMonitor,
    CCCenterPriceMonitorDependencies,
)
from .providers.ddstore.catalog import DDStoreCatalogClient
from .providers.ddstore.prices import (
    DDStorePriceMonitor,
    DDStorePriceMonitorDependencies,
)
from .providers.gjirafa50.background_monitor import Gjirafa50BackgroundPriceMonitor
from .providers.gjirafa50.catalog import Gjirafa50CatalogClient
from .providers.gjirafa50.prices import Gjirafa50PriceMonitorDependencies
from .providers.hivetec.catalog import HivetecCatalogClient
from .providers.hivetec.prices import (
    HivetecPriceMonitor,
    HivetecPriceMonitorDependencies,
)
from .providers.neksio.catalog import NeksioCatalogClient
from .providers.neksio.prices import (
    NeksioPriceMonitor,
    NeksioPriceMonitorDependencies,
)
from .providers.neptun.catalog import NeptunCatalogClient
from .providers.neptun.prices import (
    NeptunPriceMonitor,
    NeptunPriceMonitorDependencies,
)
from .providers.setec.catalog import SetecCatalogClient
from .providers.setec.prices import (
    SetecPriceMonitor,
    SetecPriceMonitorDependencies,
)
from .providers.technomarket.catalog import TechnomarketCatalogClient
from .providers.technomarket.prices import (
    TechnomarketPriceMonitor,
    TechnomarketPriceMonitorDependencies,
)
from .retries import FetchRetryPolicy, SQLiteRetryPolicy
from .transports.pazar3_catalog import Pazar3CatalogClient
from .transports.pazar3_pacing import Pazar3RequestPacer
from .transports.pazar3_price_monitor import (
    Pazar3PriceMonitor,
    Pazar3PriceMonitorDependencies,
)
from .transports.price_monitor import PriceAlertDelivery
from .transports.reklama5_catalog import Reklama5CatalogClient
from .transports.reklama5_price_monitor import (
    Reklama5PriceMonitor,
    Reklama5PriceMonitorDependencies,
)


class PriceMonitor(Protocol):
    def scan(self) -> None: ...


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
        case "anhoch":
            return AnhochPriceMonitor(
                feed,
                AnhochPriceMonitorDependencies(
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
                CCCenterPriceMonitorDependencies(
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
                DDStorePriceMonitorDependencies(
                    catalog=DDStoreCatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "hivetec":
            return HivetecPriceMonitor(
                feed,
                HivetecPriceMonitorDependencies(
                    catalog=HivetecCatalogClient(),
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
        case "neksio":
            return NeksioPriceMonitor(
                feed,
                NeksioPriceMonitorDependencies(
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
                NeptunPriceMonitorDependencies(
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
                Pazar3PriceMonitorDependencies(
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
                Reklama5PriceMonitorDependencies(
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
                SetecPriceMonitorDependencies(
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
                TechnomarketPriceMonitorDependencies(
                    catalog=TechnomarketCatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                ),
            )
        case "rss" | "xenforo" | "itmk_oglasnik":
            return None
        case unreachable:
            assert_never(unreachable)
