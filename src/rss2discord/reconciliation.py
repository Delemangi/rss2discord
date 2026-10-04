"""Offline, future-only DDStore/Hivetec reconciliation; never constructs a sender."""

import time
from collections.abc import Callable
from typing import Literal

from rss2discord.configuration import FeedConfig
from rss2discord.database_ownership import DatabaseOwnership
from rss2discord.delivery_store import DeliveryStore
from rss2discord.providers.ddstore.catalog import DDStoreCatalogClient
from rss2discord.reconciliation_models import (
    MAX_OPERATION_SECONDS,
    MAX_PLAN_AGE_SECONDS,
    ReconciliationItem,
    ReconciliationPlan,
    ReconciliationSnapshot,
    digest,
    require_safe_source_url,
    validate_dispositions,
)
from rss2discord.recovery_models import PriceSnapshot
from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.catalog_normalization import (
    CatalogObservation,
    normalize_ddstore_catalog,
    normalize_hivetec_catalog,
)
from rss2discord.transports.hivetec_catalog import HivetecCatalogClient

type CatalogFetch = Callable[
    [FeedConfig, tuple[PriceSnapshot, ...]],
    tuple[CatalogObservation, ...],
]


def _sleep(seconds: float) -> bool:
    time.sleep(seconds)
    return True


def fetch_catalog(
    feed: FeedConfig,
    persisted: tuple[PriceSnapshot, ...],
) -> tuple[CatalogObservation, ...]:
    require_safe_source_url(feed.url, feed.strategy)
    retry = FetchRetryPolicy(sleep=_sleep, on_retry=lambda _error, _delay: None)
    if feed.strategy == "ddstore":
        products = DDStoreCatalogClient().fetch_catalog(
            feed.url,
            retry_policy=retry,
            is_shutdown_requested=lambda: False,
        )
        return normalize_ddstore_catalog(feed.id, products, persisted)
    if feed.strategy == "hivetec":
        hivetec_products = HivetecCatalogClient().fetch_catalog(
            feed.url,
            retry_policy=retry,
            is_shutdown_requested=lambda: False,
        )
        return normalize_hivetec_catalog(feed.id, hivetec_products, persisted)
    raise ValueError("reconciliation supports only DDStore and Hivetec")


def create_plan(
    store: DeliveryStore,
    feed: FeedConfig,
    *,
    batch_id: int,
    batch_fingerprint: str,
    reason: str,
    catalog_fetch: CatalogFetch = fetch_catalog,
) -> ReconciliationPlan:
    started = time.monotonic()
    captured_at = int(time.time())
    plan = _build_plan(
        store,
        feed,
        batch_id=batch_id,
        batch_fingerprint=batch_fingerprint,
        reason=reason,
        captured_at=captured_at,
        catalog_fetch=catalog_fetch,
    )
    if time.monotonic() - started > MAX_OPERATION_SECONDS:
        raise ValueError("stale reconciliation evidence")
    return plan


def _build_plan(
    store: DeliveryStore,
    feed: FeedConfig,
    *,
    batch_id: int,
    batch_fingerprint: str,
    reason: str,
    captured_at: int,
    catalog_fetch: CatalogFetch,
) -> ReconciliationPlan:
    if feed.strategy not in {"ddstore", "hivetec"}:
        raise ValueError("reconciliation supports only DDStore and Hivetec")
    require_safe_source_url(feed.url, feed.strategy)
    provider: Literal["DDStore", "Hivetec"] = (
        "DDStore" if feed.strategy == "ddstore" else "Hivetec"
    )
    batch = store.load_price_batch(batch_id)
    if (
        batch is None
        or batch.feed_id != feed.id
        or batch.provider != provider
        or batch.fingerprint != batch_fingerprint
    ):
        raise ValueError("selected batch identity/fingerprint mismatch")
    if batch.status not in {"candidate", "approved", "paused", "revoked"}:
        raise ValueError("selected batch is not reconcilable backlog")
    store.require_no_open_price_claims(feed.id)
    snapshots_digest = store.price_snapshots_digest(feed.id)
    batch_digest = store.price_batch_state_digest(batch_id)
    holds_digest = store.price_holds_digest(feed.id)
    persisted = store.load_price_snapshots(feed.id)
    observations = catalog_fetch(feed, persisted)
    if not observations or len({item.product_id for item in observations}) != len(
        observations,
    ):
        raise ValueError("complete unique catalog required")
    previous = {snapshot.product_id: snapshot for snapshot in persisted}
    current = {item.product_id: item for item in observations}
    pending_ids = {item.product_id for item in batch.items if item.status == "pending"}
    held = store.held_price_product_ids(feed.id)
    items = []
    for product_id in sorted(previous.keys() | current.keys() | pending_ids | held):
        old = previous.get(product_id)
        observed = current.get(product_id)
        target = observed.snapshot if observed is not None else None
        old_value = (
            ReconciliationSnapshot.from_snapshot(old) if old is not None else None
        )
        target_value = (
            ReconciliationSnapshot.from_snapshot(target) if target is not None else None
        )
        # No price-based acceptance heuristics: pending, changed, new and
        # missing identities always require a human disposition.
        disposition: Literal["hold", "noop", "review"] = (
            "hold"
            if product_id in held
            else (
                "noop"
                if target_value is not None
                and old_value == target_value
                and product_id not in pending_ids
                else "review"
            )
        )
        items.append(
            ReconciliationItem(
                product_id=product_id,
                previous=old_value,
                target=target_value,
                context=observed.context if observed is not None else None,
                pending=product_id in pending_ids,
                already_held=product_id in held,
                disposition=disposition,
            ),
        )
    # Observe DB state again after the network operation; an uncooperative
    # legacy writer is not made safe by this check, and must be stopped.
    if (
        store.price_snapshots_digest(feed.id) != snapshots_digest
        or store.price_batch_state_digest(batch_id) != batch_digest
        or store.price_holds_digest(feed.id) != holds_digest
    ):
        raise ValueError("database changed during catalog collection")
    return ReconciliationPlan(
        feed_id=feed.id,
        provider=provider,
        source_strategy=feed.strategy,
        source_url=feed.url,  # type: ignore[arg-type]
        batch_id=batch_id,
        batch_fingerprint=batch_fingerprint,
        batch_state_digest=batch_digest,
        snapshots_digest=snapshots_digest,
        holds_digest=holds_digest,
        catalog_digest=digest(
            [
                {"id": item.product_id, "context": item.context}
                for item in sorted(observations, key=lambda item: item.product_id)
            ],
        ),
        catalog_count=len(observations),
        available_count=sum(item.snapshot is not None for item in observations),
        captured_at=captured_at,
        reason=reason,
        items=tuple(items),
    )


def apply_plan(
    store: DeliveryStore,
    feed: FeedConfig,
    plan: ReconciliationPlan,
    ownership: DatabaseOwnership,
    *,
    fingerprint: str,
    catalog_fetch: CatalogFetch = fetch_catalog,
) -> dict[str, object]:
    started = time.monotonic()
    ownership.require(store.database_path)
    require_safe_source_url(feed.url, feed.strategy)
    validate_dispositions(plan)
    if (
        fingerprint != plan.reconciliation_fingerprint
        or fingerprint != plan.fingerprint()
    ):
        raise ValueError("reconciliation fingerprint mismatch")
    if (feed.id, feed.strategy, feed.url) != (
        plan.feed_id,
        plan.source_strategy,
        plan.source_url,
    ):
        raise ValueError("reconciliation source identity mismatch")
    # Retry is a receipt lookup, not a replay, even after subsequent alerts.
    receipt = store.load_price_reconciliation(fingerprint)
    if receipt is not None:
        return receipt
    age = int(time.time()) - plan.captured_at
    if not 0 <= age <= MAX_PLAN_AGE_SECONDS:
        raise ValueError("stale reconciliation plan")
    fresh = _build_plan(
        store,
        feed,
        batch_id=plan.batch_id,
        batch_fingerprint=plan.batch_fingerprint,
        reason=plan.reason,
        captured_at=plan.captured_at,
        catalog_fetch=catalog_fetch,
    )
    # Operator decisions are the only fields not derived from fresh evidence.
    if len(plan.items) != len(fresh.items):
        raise ValueError("fresh catalog identity drift; create and review a new plan")
    comparable_items = tuple(
        item.model_copy(update={"disposition": fresh_item.disposition, "reason": ""})
        for item, fresh_item in zip(plan.items, fresh.items, strict=True)
    )
    comparable = plan.model_copy(
        update={"items": comparable_items, "reconciliation_fingerprint": ""},
    )
    if comparable != fresh:
        raise ValueError(
            "fresh catalog or database drift; create and review a new plan",
        )
    return store.apply_price_reconciliation(
        plan,
        ownership,
        operation_started_at=started,
    )
