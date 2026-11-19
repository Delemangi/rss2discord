"""Single registry composing discovery strategies and price monitors per provider."""

import logging
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from threading import Event, Lock, Thread
from types import MappingProxyType
from typing import ClassVar, Final, Protocol

import requests

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import (
    DiscordSender,
    DiscordWebhookClient,
    SleepCallback,
)
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import HealthUpdate
from rss2discord.providers.ananas.catalog import AnanasCatalogClient
from rss2discord.providers.ananas.prices import AnanasPriceMonitor
from rss2discord.providers.ananas.strategy import AnanasStrategy
from rss2discord.providers.anhoch.catalog import AnhochCatalogClient
from rss2discord.providers.anhoch.prices import AnhochPriceMonitor
from rss2discord.providers.anhoch.strategy import AnhochStrategy
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
from rss2discord.providers.pazar3.catalog import Pazar3CatalogClient
from rss2discord.providers.pazar3.pacing import Pazar3RequestPacer
from rss2discord.providers.pazar3.prices import Pazar3PriceMonitor
from rss2discord.providers.pazar3.strategy import Pazar3Strategy
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
from rss2discord.providers.woocommerce.catalog import WooCommerceCatalogClient
from rss2discord.providers.woocommerce.prices import WooCommercePriceMonitor
from rss2discord.providers.woocommerce.strategy import WooCommerceStrategy
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryInterruptedError,
    SQLiteRetryPolicy,
)
from rss2discord.transports import RSSStrategy, ScraperStrategy, XenForoStrategy
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceMonitorDependencies,
)

logger = logging.getLogger(__name__)


class PriceMonitor(Protocol):
    def scan(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DiscoveryDependencies:
    """Collaborators shared by discovery strategy construction."""

    is_shutdown_requested: Callable[[], bool]
    sleep: SleepCallback
    pazar3_pacer: Pazar3RequestPacer


@dataclass(frozen=True, slots=True)
class SharedPriceMonitorDependencies:
    snapshots: DeliveryStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery
    pazar3_pacer: Pazar3RequestPacer
    cooldown_peer_feed_ids: tuple[str, ...] = ()


DiscoveryFactory = Callable[[DiscoveryDependencies], ScraperStrategy]
PriceMonitorFactory = Callable[
    [FeedConfig, SharedPriceMonitorDependencies],
    PriceMonitor,
]


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


class BackgroundPriceMonitor:
    """Run a price scan in a worker with its own store and Discord session."""

    _scan_lock: ClassVar[Lock] = Lock()

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: SharedPriceMonitorDependencies,
        factory: PriceMonitorFactory,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies
        self._factory = factory
        self._cancel_requested: Event = Event()
        self._lock = Lock()
        self._thread: Thread | None = None

    def scan(self) -> None:
        if self._cancel_requested.is_set():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = Thread(
                target=self._run,
                name=f"price-{self._feed.id}",
                daemon=False,
            )
            self._thread.start()

    def close(self) -> None:
        self._cancel_requested.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join()

    def _run(self) -> None:
        while not self._is_shutdown_requested():
            if self._scan_lock.acquire(timeout=0.1):
                break
        else:
            return
        try:
            if self._cancel_requested.is_set():
                return
            attempted_at = int(time.time())
            started_at = time.monotonic()
            try:
                with (
                    DeliveryStore(self._dependencies.snapshots.database_path) as store,
                    requests.Session() as session,
                ):
                    if self._feed_is_blocked(store):
                        return
                    worker_dependencies = replace(
                        self._dependencies,
                        snapshots=store,
                        sender=DiscordWebhookClient(session=session),
                        fetch_retry_policy=replace(
                            self._dependencies.fetch_retry_policy,
                            sleep=self._sleep,
                        ),
                        sqlite_retry_policy=replace(
                            self._dependencies.sqlite_retry_policy,
                            sleep=self._sleep,
                        ),
                        delivery=replace(
                            self._dependencies.delivery,
                            sleep=self._sleep,
                            is_shutdown_requested=self._is_shutdown_requested,
                        ),
                    )
                    self._factory(self._feed, worker_dependencies).scan()
                    store.record_job_timing(
                        self._feed.id,
                        "price",
                        max(0, int((time.monotonic() - started_at) * 1000)),
                        0,
                    )
            except (FeedFetchInterruptedError, SQLiteRetryInterruptedError):
                return
            except Exception as error:  # noqa: RUF100  # noqa: BROAD_EXCEPT_OK
                self._record_failure(error, attempted_at, started_at)
        finally:
            self._scan_lock.release()

    def _feed_is_blocked(self, store: DeliveryStore) -> bool:
        now = int(time.time())
        return any(
            (store.get_blocked_until(feed_id) or 0) > now
            for feed_id in (
                self._feed.id,
                *self._dependencies.cooldown_peer_feed_ids,
            )
        )

    def _record_failure(
        self,
        error: Exception,
        attempted_at: int,
        started_at: float,
    ) -> None:
        cause = (
            error.cause_type
            if isinstance(error, FeedFetchError)
            else type(error).__name__
        )
        if not re.fullmatch(r"[A-Za-z]\w{0,63}", cause, flags=re.ASCII):
            cause = "FetchError"
        blocked = isinstance(error, FeedFetchError) and (
            error.cause_type in {"AccessChallenge", "BotChallenge"}
            or error.status_code == 403
        )
        try:
            with DeliveryStore(self._dependencies.snapshots.database_path) as store:
                notice = store.record_health(
                    HealthUpdate(
                        feed_id=self._feed.id,
                        job_kind="price",
                        state="blocked" if blocked else "failed",
                        cause=cause,
                        attempted_at=attempted_at,
                        success=False,
                        nonempty=False,
                        item_count=0,
                        duration_ms=max(
                            0,
                            int((time.monotonic() - started_at) * 1000),
                        ),
                    ),
                )
        except sqlite3.Error as persistence_error:
            logger.log(
                logging.ERROR,
                "Background price health persistence failed for feed %s (%s; scan %s)",
                self._feed.id,
                type(persistence_error).__name__,
                cause,
            )
            return
        if notice.should_log:
            logger.error(
                "Background price scan failed for feed %s (%s)",
                self._feed.id,
                cause,
            )

    def _is_shutdown_requested(self) -> bool:
        return (
            self._cancel_requested.is_set()
            or self._dependencies.delivery.is_shutdown_requested()
        )

    def _sleep(self, seconds: float) -> bool:
        if self._is_shutdown_requested():
            return False
        return (
            not self._cancel_requested.wait(seconds)
            and not self._dependencies.delivery.is_shutdown_requested()
        )


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """Construction entry for one feed strategy."""

    discovery: DiscoveryFactory
    price_monitor: PriceMonitorFactory | None = None


_STRATEGY_SPECS: Final[dict[str, StrategySpec]] = {
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
                database_path=dependencies.snapshots.database_path,
                cooldown_peer_feed_ids=dependencies.cooldown_peer_feed_ids,
            ),
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
    "reklama5": StrategySpec(
        discovery=lambda dependencies: Reklama5Strategy(
            is_shutdown_requested=dependencies.is_shutdown_requested,
        ),
        price_monitor=lambda feed, dependencies: BackgroundPriceMonitor(
            feed,
            dependencies,
            lambda worker_feed, worker_dependencies: Reklama5PriceMonitor(
                worker_feed,
                _monitor_dependencies(
                    Reklama5CatalogClient(),
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
    return {name: spec.discovery(dependencies) for name, spec in STRATEGY_SPECS.items()}


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
