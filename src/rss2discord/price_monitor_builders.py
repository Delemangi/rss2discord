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
type NeksioPriceMonitorFactory = Callable[
    [FeedConfig, NeksioPriceMonitorDependencies],
    PriceMonitor,
]
type NeptunPriceMonitorFactory = Callable[
    [FeedConfig, NeptunPriceMonitorDependencies],
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
    neksio: NeksioPriceMonitorFactory = NeksioPriceMonitor
    neptun: NeptunPriceMonitorFactory = NeptunPriceMonitor
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
