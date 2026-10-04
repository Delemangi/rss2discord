import sqlite3
import time
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from rss2discord.database_ownership import DatabaseOwnership, DatabaseOwnershipError
from rss2discord.delivery_store import DeliveryStore
from rss2discord.reconciliation import apply_plan, create_plan
from rss2discord.reconciliation_models import ReconciliationPlan
from rss2discord.recovery_models import PriceSnapshot
from rss2discord.transports.catalog_normalization import CatalogObservation
from tests.reconciliation_helpers import (
    catalog,
    database_state,
    fetch_current,
    reviewed,
    setup_plan,
)
from tests.test_ddstore_price_monitor import make_feed


def test_plan_requires_explicit_review_and_has_independent_fingerprint(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        before = store.load_price_snapshots("ddstore")
        plan = setup_plan(store)
        assert [item.disposition for item in plan.items] == ["review", "review"]
        assert plan.items[1].target is not None
        assert plan.items[1].target.amount == "1.05"
        assert plan.reconciliation_fingerprint == ""
        with pytest.raises(ValueError, match="explicit"):
            plan.sealed()
        sealed = reviewed(plan)
        assert sealed.fingerprint() != sealed.batch_fingerprint
        assert (
            ReconciliationPlan.model_validate_json(sealed.model_dump_json()) == sealed
        )
        assert before == ()
        assert {item.amount for item in store.load_price_snapshots("ddstore")} == {
            Decimal(100),
        }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("batch_fingerprint", "0" * 64),
        ("batch_state_digest", "0" * 64),
        ("snapshots_digest", "0" * 64),
        ("holds_digest", "0" * 64),
        ("catalog_digest", "0" * 64),
        ("source_url", "https://ddstore.mk/other"),
        ("feed_id", "other"),
        ("provider", "Hivetec"),
        ("source_strategy", "hivetec"),
        ("captured_at", 1),
    ],
)
def test_invalid_identity_state_or_stale_plan_writes_nothing(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store)).model_copy(update={field: value})
        before = database_state(store)
        with pytest.raises(
            ValueError,
            match=r"mismatch|drift|stale|invalid reconciliation source URL",
        ):
            apply_plan(
                store,
                make_feed(),
                plan.sealed(),
                ownership,
                fingerprint=plan.fingerprint(),
                catalog_fetch=fetch_current,
            )
        assert database_state(store) == before


def test_wrong_fingerprint_and_unowned_apply_write_nothing(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        before = database_state(store)
        with pytest.raises(DatabaseOwnershipError):
            apply_plan(
                store,
                make_feed(),
                plan,
                DatabaseOwnership(database),
                fingerprint=plan.fingerprint(),
                catalog_fetch=fetch_current,
            )
        with (
            DatabaseOwnership(database) as ownership,
            pytest.raises(ValueError, match="fingerprint"),
        ):
            apply_plan(
                store,
                make_feed(),
                plan,
                ownership,
                fingerprint="0" * 64,
                catalog_fetch=fetch_current,
            )
        assert database_state(store) == before


@pytest.mark.parametrize(
    "variant",
    [
        "target",
        "missing",
        "unavailable",
        "extra",
        "empty",
        "duplicate",
        "currency",
        "id",
    ],
)
def test_fresh_catalog_drift_is_zero_writes(tmp_path: Path, variant: str) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        products = catalog(89) if variant == "target" else catalog()
        if variant == "missing":
            products = products[:1]
        elif variant == "unavailable":
            products = (products[0], CatalogObservation("2", None, "zero price"))
        elif variant == "extra":
            products += (
                CatalogObservation(
                    "3",
                    PriceSnapshot("ddstore", "3", Decimal(5), "5 ден.", "MKD"),
                    "new identity",
                ),
            )
        elif variant == "empty":
            products = ()
        elif variant == "duplicate":
            products += products[:1]
        elif variant == "currency":
            products = (
                CatalogObservation(
                    "1",
                    PriceSnapshot("ddstore", "1", Decimal(90), "90 EUR", "EUR"),
                    "different currency",
                ),
                products[1],
            )
        elif variant == "id":
            products = (
                CatalogObservation("wrong", products[0].snapshot, products[0].context),
                products[1],
            )
        before = database_state(store)
        with pytest.raises(ValueError, match=r"drift|unique|MKD"):
            apply_plan(
                store,
                make_feed(),
                plan,
                ownership,
                fingerprint=plan.fingerprint(),
                catalog_fetch=lambda _feed, _old: products,
            )
        assert database_state(store) == before


def test_review_rejects_bad_currency_duplicate_ids_and_missing_accept(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        plan = reviewed(setup_plan(store))
        data = plan.model_dump(mode="json")
        data["items"][0]["target"]["currency"] = "EUR"
        with pytest.raises(ValidationError):
            ReconciliationPlan.model_validate(data)
        duplicated = plan.model_copy(update={"items": plan.items + plan.items[:1]})
        with pytest.raises(ValueError, match="unique"):
            duplicated.sealed()
        missing = plan.model_copy(
            update={
                "items": (
                    plan.items[0].model_copy(update={"target": None}),
                    plan.items[1],
                ),
            },
        )
        with pytest.raises(ValueError, match="require hold"):
            missing.sealed()


def test_apply_records_distinct_audit_preserves_delivery_manifest_and_idempotency(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        original = store.load_price_batch(plan.batch_id)
        receipt = apply_plan(
            store,
            make_feed(),
            plan,
            ownership,
            fingerprint=plan.fingerprint(),
            catalog_fetch=fetch_current,
        )
        assert receipt["accepted_count"] == receipt["held_count"] == 1
        retired = store.load_price_batch(plan.batch_id)
        assert retired is not None
        assert original is not None
        assert retired.status == "revoked"
        assert retired.items == original.items
        assert retired.fingerprint == original.fingerprint
        assert retired.summary.completed_at is None
        assert (
            retired.summary.reason == "FutureOnlyReconciliation:" + plan.fingerprint()
        )
        assert {
            snapshot.product_id: snapshot.amount
            for snapshot in store.load_price_snapshots("ddstore")
        } == {"1": Decimal(90), "2": Decimal(100)}
        assert store.held_price_product_ids("ddstore") == {"2"}
        store.upsert_price_snapshot(
            PriceSnapshot("ddstore", "1", Decimal(85), "85 ден.", "MKD"),
        )
        before = database_state(store)

        def forbidden_fetch(
            _feed: object,
            _old: object,
        ) -> tuple[CatalogObservation, ...]:
            pytest.fail(
                "idempotent retry must return a receipt without a refetch/replay",
            )

        assert (
            apply_plan(
                store,
                make_feed(),
                plan,
                ownership,
                fingerprint=plan.fingerprint(),
                catalog_fetch=forbidden_fetch,
            )
            == receipt
        )
        assert database_state(store) == before
        for table in (
            "price_reconciliations",
            "price_reconciliation_items",
            "price_product_holds",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                store._connection.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed tuple of test-owned tables
            store._connection.rollback()


def test_transaction_failure_rolls_back_everything(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        store._connection.execute(
            "CREATE TRIGGER injected_failure BEFORE INSERT ON price_product_holds BEGIN SELECT RAISE(ABORT, 'injected failure'); END",
        )
        before = database_state(store)
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            apply_plan(
                store,
                make_feed(),
                plan,
                ownership,
                fingerprint=plan.fingerprint(),
                catalog_fetch=fetch_current,
            )
        assert database_state(store) == before


def test_transaction_rechecks_snapshots_batch_and_age(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        with pytest.raises(ValueError, match="stale"):
            store.apply_price_reconciliation(
                plan,
                ownership,
                operation_started_at=time.monotonic() - 301,
            )
        store.upsert_price_snapshot(
            PriceSnapshot("ddstore", "1", Decimal(99), "99 ден.", "MKD"),
        )
        before = database_state(store)
        with pytest.raises(ValueError, match="state drift"):
            store.apply_price_reconciliation(
                plan,
                ownership,
                operation_started_at=time.monotonic(),
            )
        assert database_state(store) == before


def test_revoked_historical_open_claim_prevents_reconciliation(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        draft = setup_plan(store)
        batch = store.approve_price_change_batch(
            feed_id="ddstore",
            fingerprint=draft.batch_fingerprint,
            reason="approve old",
        )
        claim = store.claim_price_delivery_attempt(batch.batch_id, "1")
        assert claim is not None
        store.revoke_price_change_batch(
            batch_id=batch.batch_id,
            fingerprint=batch.fingerprint,
            reason="revoked with inflight claim",
        )
        before = database_state(store)
        with pytest.raises(ValueError, match="open price delivery claims"):
            create_plan(
                store,
                make_feed(),
                batch_id=batch.batch_id,
                batch_fingerprint=batch.fingerprint,
                reason="review",
                catalog_fetch=fetch_current,
            )
        with pytest.raises(ValueError, match="open price delivery claims"):
            apply_plan(
                store,
                make_feed(),
                reviewed(draft),
                ownership,
                fingerprint=reviewed(draft).fingerprint(),
                catalog_fetch=fetch_current,
            )
        assert database_state(store) == before
        assert store._connection.execute(
            "SELECT claim_open FROM price_change_batch_items WHERE batch_id = ? AND product_id = '1'",
            (batch.batch_id,),
        ).fetchone() == (1,)
