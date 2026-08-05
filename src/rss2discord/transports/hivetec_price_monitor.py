"""Sequential Hivetec price comparison and Discord delivery for one feed."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, assert_never

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult, DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.hivetec import format_hivetec_mkd, hivetec_product_metrics
from rss2discord.transports.hivetec_bounds import HIVETEC_LABEL
from rss2discord.transports.hivetec_models import HivetecProduct
from rss2discord.transports.price_monitor import PriceAlertDelivery, PriceSnapshotStore

MAX_HIVETEC_RETAINED_SNAPSHOTS: Final = 10_000
MAX_HIVETEC_PRICE_CHANGES_PER_SCAN: Final = 100


class HivetecCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[HivetecProduct, ...]: ...


class HivetecSnapshotStore(PriceSnapshotStore, Protocol):
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
        """Snapshot baselines silently and deliver bounded price changes in order."""
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        products = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        snapshots = self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.load_price_snapshots(
                self._feed.id,
                limit=MAX_HIVETEC_RETAINED_SNAPSHOTS + 1,
            ),
        )
        if len(snapshots) > MAX_HIVETEC_RETAINED_SNAPSHOTS:
            raise FeedFetchError(HIVETEC_LABEL, "SnapshotLimitExceeded")
        snapshots_by_product = {snapshot.product_id: snapshot for snapshot in snapshots}
        available_products = tuple(
            product for product in products if product.prices.current_amount > 0
        )
        retained_ids = set(snapshots_by_product)
        retained_ids.update(str(product.id) for product in available_products)
        if len(retained_ids) > MAX_HIVETEC_RETAINED_SNAPSHOTS:
            raise FeedFetchError(HIVETEC_LABEL, "SnapshotLimitExceeded")
        silent_updates: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        for product in available_products:
            current = self._snapshot(product)
            previous = snapshots_by_product.get(str(product.id))
            if previous is None:
                silent_updates.append(current)
            elif (
                previous.amount == current.amount
                and previous.currency == current.currency
            ):
                if previous.formatted != current.formatted:
                    silent_updates.append(current)
            else:
                changes.append(_PriceChange(product, previous, current))
        if len(changes) > MAX_HIVETEC_PRICE_CHANGES_PER_SCAN:
            raise FeedFetchError(HIVETEC_LABEL, "PriceChangeLimitExceeded")
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        if silent_updates:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: self._dependencies.snapshots.upsert_price_snapshots(
                    silent_updates,
                ),
            )
        self._deliver_changes(changes)

    def _deliver_changes(self, changes: list[_PriceChange]) -> None:
        delay_before_next_attempt = False
        for change in changes:
            if self._dependencies.delivery.is_shutdown_requested():
                return
            if (
                delay_before_next_attempt
                and self._dependencies.delivery.delay_between_posts > 0
                and not self._dependencies.delivery.sleep(
                    self._dependencies.delivery.delay_between_posts,
                )
            ):
                return
            result = self._dependencies.sender.send(
                self._message_for(change),
                self._dependencies.delivery.sleep,
            )
            match result:
                case DiscordDeliveryResult.DELIVERED:
                    self._persist_changed_snapshot(change.current)
                    delay_before_next_attempt = True
                case DiscordDeliveryResult.FAILED:
                    delay_before_next_attempt = False
                case DiscordDeliveryResult.INTERRUPTED:
                    return
                case unreachable:
                    assert_never(unreachable)

    def _persist_changed_snapshot(self, snapshot: PriceSnapshot) -> None:
        self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.upsert_price_snapshot(snapshot),
        )

    def _snapshot(self, product: HivetecProduct) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=str(product.id),
            amount=product.prices.current_amount,
            formatted=format_hivetec_mkd(product.prices.current_amount),
            currency=product.prices.currency_code,
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        action = (
            "decreased"
            if change.current.amount < change.previous.amount
            else "increased"
        )
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=change.product.name,
                link=change.product.permalink,
                description=(
                    f"Price {action} from {change.previous.formatted} "
                    f"to {change.current.formatted}"
                ),
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
            ),
            source_title=self._feed.name or HIVETEC_LABEL,
        )
