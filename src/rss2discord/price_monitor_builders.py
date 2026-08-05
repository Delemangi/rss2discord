from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, assert_never

from .configuration import FeedConfig
from .delivery_store import DeliveryStore
from .discord.client import DiscordSender
from .retries import FetchRetryPolicy, SQLiteRetryPolicy
from .transports.anhoch_catalog import AnhochCatalogClient
from .transports.anhoch_price_monitor import (
    AnhochPriceMonitor,
    AnhochPriceMonitorDependencies,
)
from .transports.ddstore_catalog import DDStoreCatalogClient
from .transports.ddstore_price_monitor import (
    DDStorePriceMonitor,
    DDStorePriceMonitorDependencies,
)
from .transports.gjirafa50_background_monitor import Gjirafa50BackgroundPriceMonitor
from .transports.gjirafa50_catalog import Gjirafa50CatalogClient
from .transports.gjirafa50_price_monitor import Gjirafa50PriceMonitorDependencies
from .transports.hivetec_catalog import HivetecCatalogClient
from .transports.hivetec_price_monitor import (
    HivetecPriceMonitor,
    HivetecPriceMonitorDependencies,
)
from .transports.neksio_catalog import NeksioCatalogClient
from .transports.neksio_price_monitor import (
    NeksioPriceMonitor,
    NeksioPriceMonitorDependencies,
)
from .transports.neptun_catalog import NeptunCatalogClient
from .transports.neptun_price_monitor import (
    NeptunPriceMonitor,
    NeptunPriceMonitorDependencies,
)
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
from .transports.setec_catalog import SetecCatalogClient
from .transports.setec_price_monitor import (
    SetecPriceMonitor,
    SetecPriceMonitorDependencies,
)


class PriceMonitor(Protocol):
    def scan(self) -> None: ...


type AnhochPriceMonitorFactory = Callable[
    [FeedConfig, AnhochPriceMonitorDependencies],
    PriceMonitor,
]
type DDStorePriceMonitorFactory = Callable[
    [FeedConfig, DDStorePriceMonitorDependencies],
    PriceMonitor,
]
type HivetecPriceMonitorFactory = Callable[
    [FeedConfig, HivetecPriceMonitorDependencies],
    PriceMonitor,
]
type Gjirafa50PriceMonitorFactory = Callable[
    [FeedConfig, Gjirafa50PriceMonitorDependencies],
    PriceMonitor,
]
type NeksioPriceMonitorFactory = Callable[
    [FeedConfig, NeksioPriceMonitorDependencies],
    PriceMonitor,
]
type NeptunPriceMonitorFactory = Callable[
    [FeedConfig, NeptunPriceMonitorDependencies],
    PriceMonitor,
]
type Pazar3PriceMonitorFactory = Callable[
    [FeedConfig, Pazar3PriceMonitorDependencies],
    PriceMonitor,
]
type Reklama5PriceMonitorFactory = Callable[
    [FeedConfig, Reklama5PriceMonitorDependencies],
    PriceMonitor,
]
type SetecPriceMonitorFactory = Callable[
    [FeedConfig, SetecPriceMonitorDependencies],
    PriceMonitor,
]


@dataclass(frozen=True, slots=True)
class PriceMonitorFactories:
    anhoch: AnhochPriceMonitorFactory = AnhochPriceMonitor
    ddstore: DDStorePriceMonitorFactory = DDStorePriceMonitor
    hivetec: HivetecPriceMonitorFactory = HivetecPriceMonitor
    gjirafa50: Gjirafa50PriceMonitorFactory = Gjirafa50BackgroundPriceMonitor
    neksio: NeksioPriceMonitorFactory = NeksioPriceMonitor
    neptun: NeptunPriceMonitorFactory = NeptunPriceMonitor
    pazar3: Pazar3PriceMonitorFactory = Pazar3PriceMonitor
    reklama5: Reklama5PriceMonitorFactory = Reklama5PriceMonitor
    setec: SetecPriceMonitorFactory = SetecPriceMonitor


DEFAULT_PRICE_MONITOR_FACTORIES: Final = PriceMonitorFactories()


@dataclass(frozen=True, slots=True)
class SharedPriceMonitorDependencies:
    snapshots: DeliveryStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery
    pazar3_pacer: Pazar3RequestPacer


def build_provider_price_monitor(
    feed: FeedConfig,
    dependencies: SharedPriceMonitorDependencies,
    factories: PriceMonitorFactories,
) -> PriceMonitor | None:
    match feed.strategy:
        case "anhoch":
            return factories.anhoch(
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
        case "ddstore":
            return factories.ddstore(
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
            return factories.hivetec(
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
            return factories.gjirafa50(
                feed,
                Gjirafa50PriceMonitorDependencies(
                    catalog=Gjirafa50CatalogClient(),
                    snapshots=dependencies.snapshots,
                    sender=dependencies.sender,
                    fetch_retry_policy=dependencies.fetch_retry_policy,
                    sqlite_retry_policy=dependencies.sqlite_retry_policy,
                    delivery=dependencies.delivery,
                    database_path=dependencies.snapshots.database_path,
                ),
            )
        case "neksio":
            return factories.neksio(
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
            return factories.neptun(
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
            return factories.pazar3(
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
            return factories.reklama5(
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
            return factories.setec(
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
        case "rss" | "xenforo" | "itmk_oglasnik":
            return None
        case unreachable:
            assert_never(unreachable)
