import sqlite3
import time
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.database_ownership import DatabaseOwnership
from rss2discord.delivery_store import DeliveryStore
from rss2discord.reconciliation import apply_plan, create_plan
from rss2discord.recovery_models import PriceSnapshot
from rss2discord.transports.catalog_normalization import (
    CatalogObservation,
    normalize_ddstore_catalog,
)
from tests.reconciliation_helpers import (
    database_state,
    fetch_current,
    reviewed,
    setup_plan,
)
from tests.test_ddstore_price_monitor import make_feed, make_product


def test_legacy_schema_addition_is_atomic_with_reconciliation(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        for table in (
            "price_product_holds",
            "price_reconciliation_items",
            "price_reconciliations",
        ):
            store._connection.execute(f"DROP TABLE {table}")
        store._connection.execute(
            "CREATE TRIGGER fail_baseline BEFORE UPDATE ON price_snapshots BEGIN SELECT RAISE(ABORT, 'baseline failed'); END",
        )
        store._connection.commit()
        before = database_state(store)
        with pytest.raises(sqlite3.IntegrityError, match="baseline failed"):
            apply_plan(
                store,
                make_feed(),
                plan,
                ownership,
                fingerprint=plan.fingerprint(),
                catalog_fetch=fetch_current,
            )
        assert database_state(store) == before
        store._connection.execute("DROP TRIGGER fail_baseline")
        store._connection.commit()
        receipt = apply_plan(
            store,
            make_feed(),
            plan,
            ownership,
            fingerprint=plan.fingerprint(),
            catalog_fetch=fetch_current,
        )
        assert receipt["accepted_count"] == receipt["held_count"] == 1
        assert store.held_price_product_ids("ddstore") == {"2"}


def test_plan_covers_additional_current_diff_and_missing_pending_items(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        initial = setup_plan(store)
        store.upsert_price_snapshot(
            PriceSnapshot("ddstore", "3", Decimal(100), "100 ден.", "MKD"),
        )
        products = (make_product("1", amount=90), make_product("3", amount=80))

        def fresh(
            _feed: object,
            old: tuple[PriceSnapshot, ...],
        ) -> tuple[CatalogObservation, ...]:
            return normalize_ddstore_catalog("ddstore", products, old)

        draft = create_plan(
            store,
            make_feed(),
            batch_id=initial.batch_id,
            batch_fingerprint=initial.batch_fingerprint,
            reason="review additional diff and missing item",
            catalog_fetch=fresh,
        )
        assert {item.product_id for item in draft.items} == {"1", "2", "3"}
        assert (
            next(item for item in draft.items if item.product_id == "2").target is None
        )
        assert not next(item for item in draft.items if item.product_id == "3").pending
        plan = reviewed(draft)
        apply_plan(
            store,
            make_feed(),
            plan,
            ownership,
            fingerprint=plan.fingerprint(),
            catalog_fetch=fresh,
        )
        assert {
            item.product_id: item.amount
            for item in store.load_price_snapshots("ddstore")
        } == {"1": Decimal(90), "2": Decimal(100), "3": Decimal(80)}
        assert store.held_price_product_ids("ddstore") == {"2"}


def test_age_bound_includes_fresh_collection_and_final_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        before = database_state(store)
        clock = iter((0.0, 301.0))
        monkeypatch.setattr(time, "monotonic", lambda: next(clock))
        with pytest.raises(ValueError, match="stale"):
            apply_plan(
                store,
                make_feed(),
                plan,
                ownership,
                fingerprint=plan.fingerprint(),
                catalog_fetch=fetch_current,
            )
        assert database_state(store) == before
        clock = iter((0.0, 0.0, 301.0))
        with pytest.raises(ValueError, match="stale"):
            apply_plan(
                store,
                make_feed(),
                plan,
                ownership,
                fingerprint=plan.fingerprint(),
                catalog_fetch=fetch_current,
            )
        assert database_state(store) == before


def test_batch_compare_and_swap_and_noop_do_not_fake_delivery(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        draft = setup_plan(store)
        plan = reviewed(draft)
        store.approve_price_change_batch(
            feed_id="ddstore",
            fingerprint=plan.batch_fingerprint,
            reason="state advanced after plan",
        )
        before = database_state(store)
        with pytest.raises(ValueError, match="state drift"):
            store.apply_price_reconciliation(
                plan,
                ownership,
                operation_started_at=time.monotonic(),
            )
        assert database_state(store) == before
        products = (make_product("1", amount=100), make_product("2", amount=100))

        def unchanged(
            _feed: object,
            old: tuple[PriceSnapshot, ...],
        ) -> tuple[CatalogObservation, ...]:
            return normalize_ddstore_catalog("ddstore", products, old)

        draft = create_plan(
            store,
            make_feed(),
            batch_id=draft.batch_id,
            batch_fingerprint=draft.batch_fingerprint,
            reason="pending transitions disappeared",
            catalog_fetch=unchanged,
        )
        assert all(item.disposition == "review" for item in draft.items)
        plan = draft.model_copy(
            update={
                "items": tuple(
                    item.model_copy(update={"disposition": "noop"})
                    for item in draft.items
                ),
            },
        ).sealed()
        old_snapshots = store.price_snapshots_digest("ddstore")
        receipt = apply_plan(
            store,
            make_feed(),
            plan,
            ownership,
            fingerprint=plan.fingerprint(),
            catalog_fetch=unchanged,
        )
        assert receipt["noop_count"] == 2
        assert receipt["accepted_count"] == receipt["held_count"] == 0
        assert store.price_snapshots_digest("ddstore") == old_snapshots
        batch = store.load_price_batch(draft.batch_id)
        assert batch is not None
        assert batch.status == "revoked"
        assert all(
            item.status == "pending"
            and item.attempt_count == 0
            and item.delivered_at is None
            for item in batch.items
        )
