from decimal import Decimal

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.reconciliation import CatalogFetch, create_plan
from rss2discord.reconciliation_models import ReconciliationPlan
from rss2discord.recovery_models import PriceChangeRecord, PriceSnapshot
from rss2discord.transports.catalog_normalization import (
    CatalogObservation,
    normalize_ddstore_catalog,
)
from tests.test_ddstore_price_monitor import make_feed, make_product


def catalog(amount: int = 90) -> tuple[CatalogObservation, ...]:
    return normalize_ddstore_catalog(
        "ddstore",
        (make_product("1", amount=amount), make_product("2", amount=Decimal("1.05"))),
        (),
    )


def fetch_current(
    feed: FeedConfig,
    persisted: tuple[PriceSnapshot, ...],
) -> tuple[CatalogObservation, ...]:
    del feed, persisted
    return catalog()


def setup_plan(
    store: DeliveryStore,
    catalog_fetch: CatalogFetch = fetch_current,
) -> ReconciliationPlan:
    baseline = normalize_ddstore_catalog(
        "ddstore",
        (make_product("1", amount=100), make_product("2", amount=100)),
        (),
    )
    snapshots = tuple(item.snapshot for item in baseline if item.snapshot is not None)
    store.upsert_price_snapshots(snapshots)
    current = catalog()
    records = tuple(
        PriceChangeRecord(item.product_id, old, item.snapshot)
        for item, old in zip(current, snapshots, strict=True)
        if item.snapshot is not None
    )
    batch = store.record_price_change_candidate(
        feed_id="ddstore",
        provider="DDStore",
        fingerprint=canonical_manifest_fingerprint(
            feed_id="ddstore",
            provider="DDStore",
            items=records,
        ),
        catalog_count=2,
        available_count=2,
        items=records,
    )
    return create_plan(
        store,
        make_feed(),
        batch_id=batch.batch_id,
        batch_fingerprint=batch.fingerprint,
        reason="explicit future-only review",
        catalog_fetch=catalog_fetch,
    )


def reviewed(plan: ReconciliationPlan) -> ReconciliationPlan:
    return plan.model_copy(
        update={
            "items": tuple(
                item.model_copy(
                    update={
                        "disposition": "hold" if item.product_id == "2" else "accept",
                        "reason": "suspicious identity"
                        if item.product_id == "2"
                        else "verified current price",
                    },
                )
                for item in plan.items
            ),
        },
    ).sealed()


def database_state(store: DeliveryStore) -> tuple[str, ...]:
    return tuple(store._connection.iterdump())
