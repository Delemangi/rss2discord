"""Sequential Hivetec price comparison and Discord delivery for one feed."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData
from rss2discord.recovery_models import PriceChangeRecord
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.hivetec import format_hivetec_mkd, hivetec_product_metrics
from rss2discord.transports.hivetec_bounds import HIVETEC_LABEL
from rss2discord.transports.hivetec_models import HivetecProduct
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceRecoveryStore,
    deliver_price_changes,
    finish_price_delivery,
    pause_price_fetch_failure,
    prepare_price_delivery,
    price_direction,
)

MAX_HIVETEC_RETAINED_SNAPSHOTS: Final = 10_000


class HivetecCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[HivetecProduct, ...]: ...


class HivetecSnapshotStore(PriceRecoveryStore, Protocol):
    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class HivetecPriceMonitorDependencies:
    catalog: HivetecCatalog
    snapshots: HivetecSnapshotStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _PriceChange:
    product: HivetecProduct
    previous: PriceSnapshot
    current: PriceSnapshot


class HivetecPriceMonitor:
    def __init__(
        self,
        feed: FeedConfig,
        dependencies: HivetecPriceMonitorDependencies,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies

    def scan(self) -> None:
        try:
            self._scan()
        except FeedFetchError as error:
            if not pause_price_fetch_failure(
                self._dependencies.snapshots,
                self._feed.id,
                error,
            ):
                raise

    def _scan(self) -> None:
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        products = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        persisted = self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.load_price_snapshots(
                self._feed.id,
                limit=MAX_HIVETEC_RETAINED_SNAPSHOTS + 1,
            ),
        )
        if len(persisted) > MAX_HIVETEC_RETAINED_SNAPSHOTS:
            raise FeedFetchError(HIVETEC_LABEL, "SnapshotLimitExceeded")
        by_id = {snapshot.product_id: snapshot for snapshot in persisted}
        if len({product.id for product in products}) != len(products):
            raise FeedFetchError(HIVETEC_LABEL, "ConflictingProductIDs")
        available = tuple(
            product for product in products if product.prices.current_amount > 0
        )
        if (
            len(set(by_id).union(str(product.id) for product in available))
            > MAX_HIVETEC_RETAINED_SNAPSHOTS
        ):
            raise FeedFetchError(HIVETEC_LABEL, "SnapshotLimitExceeded")
        current_by_id: dict[str, PriceSnapshot] = {}
        silent: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        for product in available:
            current = self._snapshot(product)
            current_by_id[current.product_id] = current
            previous = by_id.get(current.product_id)
            if previous is None:
                silent.append(current)
            elif (
                previous.amount != current.amount
                or previous.currency != current.currency
            ):
                changes.append(_PriceChange(product, previous, current))
            elif previous.formatted != current.formatted:
                silent.append(current)
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        store = self._dependencies.snapshots
        plan = self._dependencies.sqlite_retry_policy.execute(
            lambda: prepare_price_delivery(
                store=store,
                feed_id=self._feed.id,
                provider=HIVETEC_LABEL,
                changes=tuple(
                    PriceChangeRecord(c.current.product_id, c.previous, c.current)
                    for c in changes
                ),
                current=current_by_id,
                persisted=by_id,
                catalog_count=len(products),
            ),
        )
        if plan.blocked:
            return
        if silent and plan.allow_silent_updates:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: store.upsert_price_snapshots(silent),
            )
        changes_by_id = {change.current.product_id: change for change in changes}
        deliver_price_changes(
            (changes_by_id[product_id] for product_id in plan.selected_ids),
            self._dependencies,
            self._message_for,
            plan=plan,
        )
        finish_price_delivery(store, self._feed.id, plan, len(current_by_id))

    def _snapshot(self, product: HivetecProduct) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=str(product.id),
            amount=product.prices.current_amount,
            formatted=format_hivetec_mkd(product.prices.current_amount),
            currency=product.prices.currency_code,
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=change.product.name,
                link=change.product.permalink,
                description="",
                author="",
                timestamp=None,
                image_url=change.product.image_url,
                categories=tuple(
                    category.name for category in change.product.categories
                ),
                source_metrics=hivetec_product_metrics(
                    change.product,
                    previous_price=change.previous.formatted,
                ),
                price_direction=price_direction(change.previous, change.current),
            ),
            source_title=self._feed.name or HIVETEC_LABEL,
        )
