"""Opt-in CCCenter catalog price monitoring."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Protocol, assert_never

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult, DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData, SourceMetric
from rss2discord.recovery_models import PriceChangeRecord
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.cccenter import format_cccenter_mkd
from rss2discord.transports.cccenter_bounds import (
    CCCENTER_LABEL,
    MAX_CCCENTER_RETAINED_SNAPSHOTS,
)
from rss2discord.transports.cccenter_models import CCCenterListing, CCCenterProduct
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceDeliveryPlan,
    PriceRecoveryStore,
    finish_price_delivery,
    pause_price_fetch_failure,
    pause_price_recovery,
    persist_price_delivery,
    prepare_price_delivery,
    price_direction,
    record_price_health,
)


class CCCenterCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[CCCenterProduct, ...]: ...

    def fetch_product_details(
        self,
        listings: Sequence[CCCenterListing | CCCenterProduct],
        *,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[CCCenterProduct, ...]: ...


class CCCenterSnapshotStore(PriceRecoveryStore, Protocol):
    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class CCCenterPriceMonitorDependencies:
    catalog: CCCenterCatalog
    snapshots: CCCenterSnapshotStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _PriceChange:
    product: CCCenterProduct
    previous: PriceSnapshot
    current: PriceSnapshot


class CCCenterPriceMonitor:
    def __init__(
        self,
        feed: FeedConfig,
        dependencies: CCCenterPriceMonitorDependencies,
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
        snapshots = self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.load_price_snapshots(
                self._feed.id,
                limit=MAX_CCCENTER_RETAINED_SNAPSHOTS + 1,
            ),
        )
        if len(snapshots) > MAX_CCCENTER_RETAINED_SNAPSHOTS:
            raise FeedFetchError(CCCENTER_LABEL, "SnapshotLimitExceeded")
        by_id = {snapshot.product_id: snapshot for snapshot in snapshots}
        available = tuple(
            product
            for product in products
            if (
                product.price_status == "scalar"
                and product.current_price is not None
                and product.current_price > 0
            )
        )
        if (
            len(set(by_id).union(product.product_id for product in available))
            > MAX_CCCENTER_RETAINED_SNAPSHOTS
        ):
            raise FeedFetchError(CCCENTER_LABEL, "SnapshotLimitExceeded")
        silent: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        current_snapshots: dict[str, PriceSnapshot] = {}
        for product in available:
            current = self._snapshot(product)
            current_snapshots[product.product_id] = current
            previous = by_id.get(product.product_id)
            if previous is None:
                silent.append(current)
            elif (
                previous.amount != current.amount
                or previous.currency != current.currency
            ):
                changes.append(_PriceChange(product, previous, current))
            elif previous.formatted != current.formatted:
                silent.append(current)
        store = self._dependencies.snapshots
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        plan = self._dependencies.sqlite_retry_policy.execute(
            lambda: prepare_price_delivery(
                store=store,
                feed_id=self._feed.id,
                provider="cccenter",
                changes=tuple(
                    PriceChangeRecord(c.current.product_id, c.previous, c.current)
                    for c in changes
                ),
                current=current_snapshots,
                persisted=by_id,
                catalog_count=len(products),
            ),
        )
        if plan.blocked:
            return
        changes_by_id = {change.current.product_id: change for change in changes}
        selected = [changes_by_id[product_id] for product_id in plan.selected_ids]
        confirmed = self._confirm_changes(selected) if selected else []
        unconfirmed = len(confirmed) != len(selected)
        if unconfirmed and plan.batch_id is not None:
            pause_price_recovery(store, self._feed.id, "PriceConfirmationMismatch")
            return
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        if silent and plan.allow_silent_updates:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: self._dependencies.snapshots.upsert_price_snapshots(silent),
            )
        self._deliver(confirmed, plan)
        if unconfirmed:
            record_price_health(
                store,
                self._feed.id,
                "failed",
                "PriceConfirmationDeferred",
                len(current_snapshots),
            )
        else:
            finish_price_delivery(store, self._feed.id, plan, len(current_snapshots))

    def _confirm_changes(self, changes: Sequence[_PriceChange]) -> list[_PriceChange]:
        details = self._dependencies.catalog.fetch_product_details(
            tuple(change.product for change in changes),
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        selected_ids = {change.current.product_id for change in changes}
        by_id: dict[str, CCCenterProduct] = {}
        for product in details:
            if (
                product.product_id not in selected_ids
                or product.url != product.product_id
                or product.product_id in by_id
            ):
                raise FeedFetchError(CCCENTER_LABEL, "InvalidProductIdentity")
            by_id[product.product_id] = product
        return [
            _PriceChange(
                by_id[change.current.product_id],
                change.previous,
                change.current,
            )
            for change in changes
            if change.current.product_id in by_id
            and by_id[change.current.product_id].price_status == "scalar"
            and change.current.currency == "MKD"
            and by_id[change.current.product_id].current_price == change.current.amount
        ]

    def _snapshot(self, product: CCCenterProduct) -> PriceSnapshot:
        if product.current_price is None:
            raise FeedFetchError(CCCENTER_LABEL, "UnavailablePrice")
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=product.product_id,
            amount=product.current_price,
            formatted=format_cccenter_mkd(product.current_price),
            currency="MKD",
        )

    def _deliver(self, changes: list[_PriceChange], plan: PriceDeliveryPlan) -> None:
        delay = False
        for change in changes:
            if self._dependencies.delivery.is_shutdown_requested():
                return
            if (
                delay
                and self._dependencies.delivery.delay_between_posts > 0
                and not self._dependencies.delivery.sleep(
                    self._dependencies.delivery.delay_between_posts,
                )
            ):
                return
            if self._dependencies.delivery.is_shutdown_requested():
                return
            result = self._dependencies.sender.send(
                self._message(change),
                self._dependencies.delivery.sleep,
            )
            match result:
                case DiscordDeliveryResult.DELIVERED:
                    self._dependencies.sqlite_retry_policy.execute(
                        partial(
                            persist_price_delivery,
                            self._dependencies.snapshots,
                            plan,
                            change.current,
                        ),
                    )
                    delay = True
                case DiscordDeliveryResult.FAILED:
                    delay = False
                case DiscordDeliveryResult.INTERRUPTED:
                    return
                case other:
                    assert_never(other)

    def _message(self, change: _PriceChange) -> WebhookMessage:
        product = change.product
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=product.name,
                link=product.url,
                description="",
                author="",
                timestamp=None,
                image_url=product.image_url,
                categories=product.categories,
                source_metrics=(
                    SourceMetric("Price", format_cccenter_mkd(product.current_price))
                    if product.current_price is not None
                    else SourceMetric("Price", "Unavailable"),
                    SourceMetric("Previous", change.previous.formatted, prior=True),
                    *(
                        (
                            SourceMetric(
                                "Original",
                                format_cccenter_mkd(product.original_price),
                            ),
                        )
                        if product.original_price is not None
                        and product.original_price != product.current_price
                        else ()
                    ),
                    SourceMetric(
                        "Stock",
                        "In stock" if product.is_in_stock else "Out of stock",
                    ),
                    *((SourceMetric("SKU", product.sku),) if product.sku else ()),
                ),
                price_direction=price_direction(change.previous, change.current),
            ),
            source_title=self._feed.name or CCCENTER_LABEL,
        )
