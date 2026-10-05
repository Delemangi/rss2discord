import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.database_ownership import DatabaseOwnership
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.reconciliation import apply_plan, create_plan
from rss2discord.reconciliation_models import ReconciliationPlan
from rss2discord.recovery_models import PriceChangeRecord, PriceSnapshot
from rss2discord.retries import SQLiteRetryPolicy
from rss2discord.transports.catalog_normalization import (
    CatalogObservation,
    normalize_ddstore_catalog,
)
from tests.reconciliation_helpers import setup_plan
from tests.test_ddstore_price_monitor import make_feed, make_product


def test_legacy_holds_migrate_to_sticky_review_and_read_only_does_not_migrate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.db"
    with DeliveryStore(database) as store:
        plan = setup_plan(store)
        store._connection.execute(
            "INSERT INTO price_reconciliations (reconciliation_fingerprint, feed_id, batch_id, reason, plan_json, accepted_count, held_count, noop_count) "
            "VALUES (?, 'ddstore', ?, 'legacy', '{}', 0, 1, 0)",
            ("a" * 64, plan.batch_id),
        )
        store._connection.execute(
            "INSERT INTO price_product_holds (feed_id, product_id, reconciliation_fingerprint, reason) VALUES ('ddstore', '2', ?, 'legacy')",
            ("a" * 64,),
        )
        store._connection.commit()
        store._connection.execute("DROP TRIGGER price_product_holds_guarded_delete")
        store._connection.execute("DROP TRIGGER price_product_holds_immutable_update")
        store._connection.execute("DROP TABLE price_hold_releases")
        store._connection.execute("ALTER TABLE price_product_holds DROP COLUMN kind")
        store._connection.commit()

    with DeliveryStore(database, read_only=True, initialize=False) as readonly:
        assert "kind" not in {
            row[1]
            for row in readonly._connection.execute(
                "PRAGMA table_info(price_product_holds)",
            )
        }
        assert readonly.list_price_product_holds("ddstore")[0]["kind"] == "review"
        assert isinstance(readonly.price_holds_digest("ddstore", version=1), str)
    with DeliveryStore(database) as migrated:
        (hold,) = migrated.list_price_product_holds("ddstore")
        assert hold["kind"] == "review"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            migrated._connection.execute(
                "DELETE FROM price_product_holds WHERE feed_id = 'ddstore' AND product_id = '2'",
            )
    with DeliveryStore(database) as reopened:
        assert reopened.list_price_product_holds("ddstore")[0]["kind"] == "review"


def test_absent_baseline_refuses_concurrent_snapshot_insert_without_overwrite(
    tmp_path: Path,
) -> None:
    database = tmp_path / "concurrent-first-baseline.db"
    with DeliveryStore(database) as store:
        plan = apply_defer_plan(
            store,
            database,
            include_unpriced_product=True,
        )
        other = sqlite3.connect(database)
        other.execute(
            "INSERT INTO price_snapshots (feed_id, product_id, amount, formatted, currency) "
            "VALUES ('ddstore', '3', '25', '25 ден.', 'MKD')",
        )
        other.commit()
        other.close()
        context = normalize_ddstore_catalog(
            "ddstore",
            (make_product("3", amount=50),),
            (),
        )[0].context
        assert not store.restore_availability_hold(
            feed_id="ddstore",
            provider="DDStore",
            product_id="3",
            origin_fingerprint=plan.reconciliation_fingerprint,
            previous=None,
            observed=PriceSnapshot("ddstore", "3", Decimal(50), "50 ден.", "MKD"),
            context=context,
            source="validated_full_catalog",
            operation_started_at=time.monotonic(),
        )
        assert (
            store._connection.execute(
                "SELECT amount FROM price_snapshots WHERE feed_id = 'ddstore' AND product_id = '3'",
            ).fetchone()[0]
            == "25"
        )
        assert store.held_price_product_ids("ddstore") == frozenset({"2", "3"})
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases WHERE product_id = '3'",
            ).fetchone()[0]
            == 0
        )


def test_first_baseline_restore_rolls_back_receipt_snapshot_and_hold(
    tmp_path: Path,
) -> None:
    database = tmp_path / "first-baseline-rollback.db"
    with DeliveryStore(database) as store:
        plan = apply_defer_plan(store, database, include_unpriced_product=True)
        context = normalize_ddstore_catalog(
            "ddstore",
            (make_product("3", amount=50),),
            (),
        )[0].context
        store._connection.execute(
            "CREATE TRIGGER fail_first_snapshot AFTER INSERT ON price_snapshots "
            "WHEN NEW.feed_id = 'ddstore' AND NEW.product_id = '3' "
            "BEGIN SELECT RAISE(ABORT, 'first snapshot failed'); END",
        )
        store._connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="first snapshot failed"):
            store.restore_availability_hold(
                feed_id="ddstore",
                provider="DDStore",
                product_id="3",
                origin_fingerprint=plan.reconciliation_fingerprint,
                previous=None,
                observed=PriceSnapshot("ddstore", "3", Decimal(50), "50 ден.", "MKD"),
                context=context,
                source="validated_full_catalog",
                operation_started_at=time.monotonic(),
            )
        assert store.held_price_product_ids("ddstore") == frozenset({"2", "3"})
        assert (
            store._connection.execute(
                "SELECT 1 FROM price_snapshots WHERE feed_id = 'ddstore' AND product_id = '3'",
            ).fetchone()
            is None
        )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases WHERE product_id = '3'",
            ).fetchone()[0]
            == 0
        )


def test_availability_release_is_audited_atomic_and_retry_safe(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        plan = apply_defer_plan(store, database)
        origin = plan.reconciliation_fingerprint
        previous = next(
            s for s in store.load_price_snapshots("ddstore") if s.product_id == "2"
        )
        observed = PriceSnapshot("ddstore", "2", Decimal(75), "75 ден.", "MKD")
        context = normalize_ddstore_catalog(
            "ddstore",
            (make_product("2", amount=75),),
            (),
        )[0].context

        def release() -> bool:
            return store.restore_availability_hold(
                feed_id="ddstore",
                provider="DDStore",
                product_id="2",
                origin_fingerprint=origin,
                previous=previous,
                observed=observed,
                context=context,
                source="validated_full_catalog",
                operation_started_at=time.monotonic(),
            )

        failures = (
            "CREATE TRIGGER injected_restore_failure AFTER INSERT ON price_hold_releases",
            "CREATE TRIGGER injected_restore_failure AFTER UPDATE ON price_snapshots",
            "CREATE TRIGGER injected_restore_failure AFTER DELETE ON price_product_holds",
        )
        for create_trigger in failures:
            store._connection.execute(
                create_trigger
                + " BEGIN SELECT RAISE(ABORT, 'restore write failed'); END",
            )
            store._connection.commit()
            with pytest.raises(sqlite3.IntegrityError, match="restore write failed"):
                release()
            assert (
                store._connection.execute(
                    "SELECT COUNT(*) FROM price_hold_releases",
                ).fetchone()[0]
                == 0
            )
            assert store.held_price_product_ids("ddstore") == frozenset({"2"})
            assert (
                next(
                    s
                    for s in store.load_price_snapshots("ddstore")
                    if s.product_id == "2"
                )
                == previous
            )
            store._connection.execute("DROP TRIGGER injected_restore_failure")
            store._connection.commit()
        assert release()
        newer = PriceSnapshot("ddstore", "2", Decimal(80), "80 ден.", "MKD")
        store.upsert_price_snapshot(newer)
        assert not release()
        assert store.held_price_product_ids("ddstore") == frozenset()
        assert (
            next(
                s for s in store.load_price_snapshots("ddstore") if s.product_id == "2"
            )
            == newer
        )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 1
        )
        columns = store._connection.execute(
            "PRAGMA table_info(price_hold_releases)",
        ).fetchall()
        assert tuple(
            row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]
        ) == (
            "feed_id",
            "product_id",
            "originating_reconciliation_fingerprint",
        )
        with pytest.raises(ValueError, match="version 1"):
            store.price_holds_digest("ddstore", version=1)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._connection.execute(
                "UPDATE price_hold_releases SET context = 'changed'",
            )
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute("DELETE FROM price_hold_releases")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._connection.execute(
                "DELETE FROM price_reconciliations WHERE reconciliation_fingerprint = ?",
                (origin,),
            )


def test_availability_origins_are_feed_scoped_and_cross_feed_release_is_refused(
    tmp_path: Path,
) -> None:
    database = tmp_path / "feeds.db"
    with DeliveryStore(database) as store:
        plan = apply_defer_plan(store, database)
        store._connection.execute(
            "INSERT INTO price_snapshots (feed_id, product_id, amount, formatted, currency) "
            "VALUES ('other-feed', '2', '100', '100 ден.', 'MKD')",
        )
        store._connection.execute(
            "INSERT INTO price_product_holds (feed_id, product_id, reconciliation_fingerprint, reason, kind) "
            "VALUES ('other-feed', '2', ?, 'foreign-feed row', 'availability')",
            (plan.reconciliation_fingerprint,),
        )
        store._connection.commit()
        assert [
            row["product_id"] for row in store.availability_hold_origins("ddstore")
        ] == ["2"]
        assert [
            row["product_id"] for row in store.availability_hold_origins("other-feed")
        ] == ["2"]
        assert not store.restore_availability_hold(
            feed_id="other-feed",
            provider="DDStore",
            product_id="2",
            origin_fingerprint=plan.reconciliation_fingerprint,
            previous=PriceSnapshot("other-feed", "2", Decimal(100), "100 ден.", "MKD"),
            observed=PriceSnapshot("other-feed", "2", Decimal(75), "75 ден.", "MKD"),
            context=normalize_ddstore_catalog(
                "ddstore",
                (make_product("2", amount=75),),
                (),
            )[0].context,
            source="validated_full_catalog",
            operation_started_at=time.monotonic(),
        )
        assert (
            store._connection.execute(
                "SELECT amount FROM price_snapshots WHERE feed_id = 'other-feed' AND product_id = '2'",
            ).fetchone()[0]
            == "100"
        )
        assert store.held_price_product_ids("ddstore") == frozenset({"2"})


def apply_defer_plan(
    store: DeliveryStore,
    database: Path,
    *,
    review_product_1: bool = False,
    include_unpriced_product: bool = False,
) -> ReconciliationPlan:
    draft = setup_plan(store)

    def missing(
        _feed: object,
        persisted: tuple[PriceSnapshot, ...],
    ) -> tuple[CatalogObservation, ...]:
        return normalize_ddstore_catalog(
            "ddstore",
            (
                (make_product("1", amount=90), make_product("3", amount=0))
                if include_unpriced_product
                else (make_product("1", amount=90),)
            ),
            persisted,
        )

    draft = create_plan(
        store,
        make_feed(),
        batch_id=draft.batch_id,
        batch_fingerprint=draft.batch_fingerprint,
        reason="availability defer test",
        catalog_fetch=missing,
        version=2,
    )
    plan = draft.model_copy(
        update={
            "items": tuple(
                item.model_copy(
                    update={
                        "disposition": (
                            "defer"
                            if item.product_id == "2"
                            or (include_unpriced_product and item.product_id == "3")
                            else "hold"
                            if review_product_1
                            else "accept"
                        ),
                        "reason": "unpriced" if item.product_id == "2" else "verified",
                    },
                )
                for item in draft.items
            ),
        },
    ).sealed()
    with DatabaseOwnership(database) as ownership:
        receipt = apply_plan(
            store,
            make_feed(),
            plan,
            ownership,
            fingerprint=plan.fingerprint(),
            catalog_fetch=missing,
        )
    assert receipt["plan_json"] == plan.model_dump_json()
    assert receipt["feed_id"] == "ddstore"
    assert receipt["reconciliation_fingerprint"] == plan.reconciliation_fingerprint
    stored_disposition, stored_item_json = store._connection.execute(
        "SELECT disposition, item_json FROM price_reconciliation_items "
        "WHERE reconciliation_fingerprint = ? AND product_id = '2'",
        (plan.reconciliation_fingerprint,),
    ).fetchone()
    assert stored_disposition == "hold"
    assert json.loads(stored_item_json)["disposition"] == "defer"
    return plan


@pytest.mark.parametrize("status", ["candidate", "approved", "paused"])
def test_restore_rechecks_nonterminal_batch_under_store_lock(
    tmp_path: Path,
    status: str,
) -> None:
    database = tmp_path / f"{status}.db"
    with DeliveryStore(database) as store:
        plan = apply_defer_plan(store, database)
        store._connection.execute(
            "UPDATE price_change_batches SET status = ? WHERE batch_id = ?",
            (status, plan.batch_id),
        )
        store._connection.commit()
        previous = next(
            s for s in store.load_price_snapshots("ddstore") if s.product_id == "2"
        )
        observed = PriceSnapshot("ddstore", "2", Decimal(75), "75 ден.", "MKD")
        context = normalize_ddstore_catalog(
            "ddstore",
            (make_product("2", amount=75),),
            (),
        )[0].context
        assert not store.restore_availability_hold(
            feed_id="ddstore",
            provider="DDStore",
            product_id="2",
            origin_fingerprint=plan.reconciliation_fingerprint,
            previous=previous,
            observed=observed,
            context=context,
            source="validated_full_catalog",
            operation_started_at=time.monotonic(),
        )
        assert store.held_price_product_ids("ddstore") == frozenset({"2"})
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 0
        )


def test_old_release_origin_cannot_release_a_later_rehold(tmp_path: Path) -> None:
    database = tmp_path / "rehold.db"
    with DeliveryStore(database) as store:
        old_plan = apply_defer_plan(store, database)
        previous = next(
            s for s in store.load_price_snapshots("ddstore") if s.product_id == "2"
        )
        observed = PriceSnapshot("ddstore", "2", Decimal(75), "75 ден.", "MKD")
        context = normalize_ddstore_catalog(
            "ddstore",
            (make_product("2", amount=75),),
            (),
        )[0].context
        assert store.restore_availability_hold(
            feed_id="ddstore",
            provider="DDStore",
            product_id="2",
            origin_fingerprint=old_plan.reconciliation_fingerprint,
            previous=previous,
            observed=observed,
            context=context,
            source="validated_full_catalog",
            operation_started_at=time.monotonic(),
        )

        old_current = next(
            s for s in store.load_price_snapshots("ddstore") if s.product_id == "2"
        )
        next_current = PriceSnapshot("ddstore", "2", Decimal(80), "80 ден.", "MKD")
        record = PriceChangeRecord("2", old_current, next_current)
        batch = store.record_price_change_candidate(
            feed_id="ddstore",
            provider="DDStore",
            fingerprint=canonical_manifest_fingerprint(
                feed_id="ddstore",
                provider="DDStore",
                items=(record,),
            ),
            catalog_count=2,
            available_count=1,
            items=(record,),
        )

        def missing(
            _feed: object,
            persisted: tuple[PriceSnapshot, ...],
        ) -> tuple[CatalogObservation, ...]:
            return normalize_ddstore_catalog(
                "ddstore",
                (make_product("1", amount=90),),
                persisted,
            )

        draft = create_plan(
            store,
            make_feed(),
            batch_id=batch.batch_id,
            batch_fingerprint=batch.fingerprint,
            reason="rehold test",
            catalog_fetch=missing,
            version=2,
        )
        new_plan = draft.model_copy(
            update={
                "items": tuple(
                    item.model_copy(
                        update={
                            "disposition": "defer"
                            if item.product_id == "2"
                            else "noop",
                            "reason": "unpriced inventory"
                            if item.product_id == "2"
                            else "",
                        },
                    )
                    for item in draft.items
                ),
            },
        ).sealed()
        with DatabaseOwnership(database) as ownership:
            apply_plan(
                store,
                make_feed(),
                new_plan,
                ownership,
                fingerprint=new_plan.fingerprint(),
                catalog_fetch=missing,
            )
        snapshot_before_retry = next(
            s for s in store.load_price_snapshots("ddstore") if s.product_id == "2"
        )
        assert (
            new_plan.reconciliation_fingerprint != old_plan.reconciliation_fingerprint
        )
        assert not store.restore_availability_hold(
            feed_id="ddstore",
            provider="DDStore",
            product_id="2",
            origin_fingerprint=old_plan.reconciliation_fingerprint,
            previous=old_current,
            observed=PriceSnapshot("ddstore", "2", Decimal(70), "70 ден.", "MKD"),
            context=normalize_ddstore_catalog(
                "ddstore",
                (make_product("2", amount=70),),
                (),
            )[0].context,
            source="validated_full_catalog",
            operation_started_at=time.monotonic(),
        )
        assert store.held_price_product_ids("ddstore") == frozenset({"2"})
        assert (
            next(
                s for s in store.load_price_snapshots("ddstore") if s.product_id == "2"
            )
            == snapshot_before_retry
        )
        assert store.restore_availability_hold(
            feed_id="ddstore",
            provider="DDStore",
            product_id="2",
            origin_fingerprint=new_plan.reconciliation_fingerprint,
            previous=snapshot_before_retry,
            observed=PriceSnapshot("ddstore", "2", Decimal(70), "70 ден.", "MKD"),
            context=normalize_ddstore_catalog(
                "ddstore",
                (make_product("2", amount=70),),
                (),
            )[0].context,
            source="validated_full_catalog",
            operation_started_at=time.monotonic(),
        )
        assert store.held_price_product_ids("ddstore") == frozenset()
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize(
    ("amount", "currency", "context"),
    [
        (Decimal(0), "MKD", "valid"),
        (Decimal("NaN"), "MKD", "valid"),
        (Decimal(75), "USD", "valid"),
        (Decimal(75), "MKD", "{}"),
    ],
)
def test_invalid_restoration_evidence_has_no_store_effects(
    tmp_path: Path,
    amount: Decimal,
    currency: str,
    context: str,
) -> None:
    database = tmp_path / "invalid.db"
    with DeliveryStore(database) as store:
        plan = apply_defer_plan(store, database)
        previous = next(
            s for s in store.load_price_snapshots("ddstore") if s.product_id == "2"
        )
        valid_context = normalize_ddstore_catalog(
            "ddstore",
            (make_product("2", amount=75),),
            (),
        )[0].context
        observed = PriceSnapshot("ddstore", "2", amount, "75 ден.", currency)
        with pytest.raises(
            ValueError,
            match=r"availability restoration|invalid reconciliation context",
        ):
            store.restore_availability_hold(
                feed_id="ddstore",
                provider="DDStore",
                product_id="2",
                origin_fingerprint=plan.reconciliation_fingerprint,
                previous=previous,
                observed=observed,
                context=valid_context if context == "valid" else context,
                source="validated_full_catalog",
                operation_started_at=time.monotonic(),
            )
        assert store.held_price_product_ids("ddstore") == frozenset({"2"})
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 0
        )


def test_restore_rechecks_revoked_historical_open_claim(tmp_path: Path) -> None:
    database = tmp_path / "claim.db"
    with DeliveryStore(database) as store:
        plan = apply_defer_plan(store, database)
        store._connection.execute(
            "UPDATE price_change_batch_items SET claim_open = 1 WHERE batch_id = ? AND product_id = '2'",
            (plan.batch_id,),
        )
        store._connection.commit()
        previous = next(
            s for s in store.load_price_snapshots("ddstore") if s.product_id == "2"
        )
        observed = PriceSnapshot("ddstore", "2", Decimal(75), "75 ден.", "MKD")
        context = normalize_ddstore_catalog(
            "ddstore",
            (make_product("2", amount=75),),
            (),
        )[0].context
        with pytest.raises(ValueError, match="historical revoked batches"):
            store.restore_availability_hold(
                feed_id="ddstore",
                provider="DDStore",
                product_id="2",
                origin_fingerprint=plan.reconciliation_fingerprint,
                previous=previous,
                observed=observed,
                context=context,
                source="validated_full_catalog",
                operation_started_at=time.monotonic(),
            )
        assert store.held_price_product_ids("ddstore") == frozenset({"2"})
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 0
        )


def test_busy_retry_keeps_release_origin_and_commits_once(tmp_path: Path) -> None:
    database = tmp_path / "busy.db"
    with DeliveryStore(database) as store:
        plan = apply_defer_plan(store, database)
        previous = next(
            s for s in store.load_price_snapshots("ddstore") if s.product_id == "2"
        )
        observed = PriceSnapshot("ddstore", "2", Decimal(75), "75 ден.", "MKD")
        context = normalize_ddstore_catalog(
            "ddstore",
            (make_product("2", amount=75),),
            (),
        )[0].context
        blocker = sqlite3.connect(database, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        store._connection.execute("PRAGMA busy_timeout = 0")
        sleeps: list[float] = []

        def release_lock(seconds: float) -> bool:
            sleeps.append(seconds)
            blocker.rollback()
            return True

        retry = SQLiteRetryPolicy(
            sleep=release_lock,
            on_retry=lambda error, delay: None,
        )
        operation_started_at = time.monotonic()
        result = retry.execute(
            lambda: store.restore_availability_hold(
                feed_id="ddstore",
                provider="DDStore",
                product_id="2",
                origin_fingerprint=plan.reconciliation_fingerprint,
                previous=previous,
                observed=observed,
                context=context,
                source="validated_full_catalog",
                operation_started_at=operation_started_at,
            ),
        )
        blocker.close()
        assert result
        assert sleeps == [5.0]
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 1
        )


def test_busy_retry_cannot_renew_observation_freshness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "busy-stale.db"
    with DeliveryStore(database) as store:
        plan = apply_defer_plan(store, database)
        previous = next(
            snapshot
            for snapshot in store.load_price_snapshots("ddstore")
            if snapshot.product_id == "2"
        )
        observed = PriceSnapshot("ddstore", "2", Decimal(75), "75 ден.", "MKD")
        context = normalize_ddstore_catalog(
            "ddstore",
            (make_product("2", amount=75),),
            (),
        )[0].context
        clock = [100.0]
        monkeypatch.setattr(
            "rss2discord.delivery_store.time.monotonic",
            lambda: clock[0],
        )
        operation_started_at = time.monotonic()
        blocker = sqlite3.connect(database, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        store._connection.execute("PRAGMA busy_timeout = 0")

        def expire_and_release(seconds: float) -> bool:
            assert seconds == 5.0
            clock[0] += 301
            blocker.rollback()
            return True

        retry = SQLiteRetryPolicy(
            sleep=expire_and_release,
            on_retry=lambda error, delay: None,
        )
        with pytest.raises(ValueError, match="stale availability restoration"):
            retry.execute(
                lambda: store.restore_availability_hold(
                    feed_id="ddstore",
                    provider="DDStore",
                    product_id="2",
                    origin_fingerprint=plan.reconciliation_fingerprint,
                    previous=previous,
                    observed=observed,
                    context=context,
                    source="validated_full_catalog",
                    operation_started_at=operation_started_at,
                ),
            )
        blocker.close()
        assert store.held_price_product_ids("ddstore") == frozenset({"2"})
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 0
        )
