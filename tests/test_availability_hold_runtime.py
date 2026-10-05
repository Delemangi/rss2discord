from dataclasses import replace
from pathlib import Path

import pytest

from rss2discord.database_ownership import DatabaseOwnership
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.reconciliation import apply_plan, create_plan
from rss2discord.recovery_models import PriceBatch, PriceChangeRecord, PriceSnapshot
from rss2discord.transports import price_monitor as price_monitor_module
from rss2discord.transports.catalog_normalization import (
    CatalogObservation,
    hivetec_snapshot,
    normalize_hivetec_catalog,
)
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.setec_price_monitor_helpers import RecordingSender
from tests.test_availability_hold_store import apply_defer_plan
from tests.test_ddstore_price_monitor import (
    CatalogStub,
    make_feed,
    make_monitor,
    make_product,
)
from tests.test_hivetec_price_monitor import (
    CatalogStub as HivetecCatalogStub,
)
from tests.test_hivetec_price_monitor import (
    feed as hivetec_feed,
)
from tests.test_hivetec_price_monitor import (
    monitor as make_hivetec_monitor,
)
from tests.test_hivetec_price_monitor import (
    product as make_hivetec_product,
)


def test_returning_availability_hold_restores_silently_then_real_change_alerts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        apply_defer_plan(store, database, include_unpriced_product=True)
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
        catalogs = CatalogStub(
            [
                (
                    make_product("1", amount=90),
                    make_product("2", amount=75, stock_status="OUT_OF_STOCK"),
                    make_product("3", amount=50),
                ),
                (
                    make_product("1", amount=90),
                    make_product("2", amount=75),
                    make_product("3", amount=50),
                ),
                (
                    make_product("1", amount=90),
                    make_product("2", amount=70),
                    make_product("3", amount=50),
                ),
            ],
        )
        monitor = make_monitor(
            make_feed(),
            catalogs,
            store,
            sender,
        )
        monitor.scan()
        assert sender.messages == []
        assert store.held_price_product_ids("ddstore") == frozenset()
        assert (
            next(
                s.amount
                for s in store.load_price_snapshots("ddstore")
                if s.product_id == "2"
            )
            == 75
        )
        first_return_receipt = store._connection.execute(
            "SELECT previous_amount, previous_formatted, previous_currency "
            "FROM price_hold_releases WHERE product_id = '3'",
        ).fetchone()
        assert first_return_receipt == (None, None, None)
        assert any(
            s.product_id == "3" and s.amount == 50
            for s in store.load_price_snapshots("ddstore")
        )
        monitor.scan()
        assert sender.messages == []
        monitor.scan()
        assert len(sender.messages) == 1


def test_missing_return_id_and_shutdown_do_not_release_or_advance_baseline(
    tmp_path: Path,
) -> None:
    database = tmp_path / "deferred.db"
    with DeliveryStore(database) as store:
        apply_defer_plan(store, database)
        before = store.price_snapshots_digest("ddstore")
        sender = RecordingSender([])
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(make_product("1", amount=90),)]),
            store,
            sender,
        )
        monitor.scan()
        assert store.held_price_product_ids("ddstore") == frozenset({"2"})
        assert store.price_snapshots_digest("ddstore") == before
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 0
        )

        checks = 0

        def shutdown_before_restore() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 4

        monitor._dependencies = replace(
            monitor._dependencies,
            catalog=CatalogStub(
                [(make_product("1", amount=90), make_product("2", amount=75))],
            ),
            delivery=PriceAlertDelivery(
                sleep=lambda seconds: True,
                delay_between_posts=0,
                is_shutdown_requested=shutdown_before_restore,
            ),
        )
        monitor.scan()
        assert checks == 4
        assert store.held_price_product_ids("ddstore") == frozenset({"2"})
        assert store.price_snapshots_digest("ddstore") == before
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 0
        )
        assert sender.messages == []


def test_duplicate_catalog_identity_cannot_release_availability_hold(
    tmp_path: Path,
) -> None:
    database = tmp_path / "duplicate.db"
    with DeliveryStore(database) as store:
        apply_defer_plan(store, database)
        before = store.price_snapshots_digest("ddstore")
        duplicate = make_product("2", amount=75)
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(make_product("1", amount=90), duplicate, duplicate)]),
            store,
            RecordingSender([]),
        )
        with pytest.raises(FeedFetchError):
            monitor.scan()
        assert store.held_price_product_ids("ddstore") == frozenset({"2"})
        assert store.price_snapshots_digest("ddstore") == before
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 0
        )


def test_catalog_to_plan_elapsed_budget_is_not_reset_before_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "stale-observation.db"
    with DeliveryStore(database) as store:
        apply_defer_plan(store, database, include_unpriced_product=True)
        before = store.price_snapshots_digest("ddstore")
        clock = [100.0]
        monkeypatch.setattr(price_monitor_module.time, "monotonic", lambda: clock[0])
        load_active = store.load_active_price_batch

        def expire_during_plan(feed_id: str) -> PriceBatch | None:
            batch = load_active(feed_id)
            clock[0] += 301
            return batch

        monkeypatch.setattr(store, "load_active_price_batch", expire_during_plan)
        monitor = make_monitor(
            make_feed(),
            CatalogStub(
                [
                    (
                        make_product("1", amount=90),
                        make_product("2", amount=75),
                        make_product("3", amount=50),
                    ),
                ],
            ),
            store,
            RecordingSender([]),
        )
        with pytest.raises(ValueError, match="stale availability restoration"):
            monitor.scan()
        assert store.held_price_product_ids("ddstore") == frozenset({"2", "3"})
        assert store.price_snapshots_digest("ddstore") == before
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 0
        )


def test_second_return_cannot_renew_scan_freshness_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "second-stale.db"
    with DeliveryStore(database) as store:
        apply_defer_plan(store, database, include_unpriced_product=True)
        clock = [100.0]
        monkeypatch.setattr(price_monitor_module.time, "monotonic", lambda: clock[0])
        restore = store.restore_availability_hold
        calls = 0

        def expire_after_first_release(
            *,
            feed_id: str,
            provider: str,
            product_id: str,
            origin_fingerprint: str,
            previous: PriceSnapshot | None,
            observed: PriceSnapshot,
            context: str,
            source: str,
            operation_started_at: float,
        ) -> bool:
            nonlocal calls
            calls += 1
            result = restore(
                feed_id=feed_id,
                provider=provider,
                product_id=product_id,
                origin_fingerprint=origin_fingerprint,
                previous=previous,
                observed=observed,
                context=context,
                source=source,
                operation_started_at=operation_started_at,
            )
            if calls == 1:
                clock[0] += 301
            return result

        monkeypatch.setattr(
            store,
            "restore_availability_hold",
            expire_after_first_release,
        )
        monitor = make_monitor(
            make_feed(),
            CatalogStub(
                [
                    (
                        make_product("1", amount=90),
                        make_product("2", amount=75),
                        make_product("3", amount=50),
                    ),
                ],
            ),
            store,
            RecordingSender([]),
        )
        with pytest.raises(ValueError, match="stale availability restoration"):
            monitor.scan()
        assert calls == 2
        assert store.held_price_product_ids("ddstore") == frozenset({"3"})
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 1
        )


def test_review_and_availability_holds_coexist_but_only_availability_releases(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mixed.db"
    with DeliveryStore(database) as store:
        apply_defer_plan(store, database, review_product_1=True)
        monitor = make_monitor(
            make_feed(),
            CatalogStub(
                [(make_product("1", amount=80), make_product("2", amount=75))],
            ),
            store,
            RecordingSender([]),
        )
        monitor.scan()
        holds = {
            hold["product_id"]: hold["kind"]
            for hold in store.list_price_product_holds("ddstore")
        }
        assert holds == {"1": "review"}
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_hold_releases",
            ).fetchone()[0]
            == 1
        )


def test_hivetec_applied_defer_restores_silently_then_alerts_on_later_change(
    tmp_path: Path,
) -> None:
    database = tmp_path / "hivetec.db"
    feed = hivetec_feed()
    with DeliveryStore(database) as store:
        previous = tuple(
            hivetec_snapshot("hivetec", item)
            for item in (
                make_hivetec_product(1, "10000"),
                make_hivetec_product(2, "10000"),
            )
        )
        store.upsert_price_snapshots(previous)
        current = tuple(
            PriceChangeRecord(
                old.product_id,
                old,
                hivetec_snapshot(
                    "hivetec",
                    make_hivetec_product(int(old.product_id), "9000"),
                ),
            )
            for old in previous
        )
        batch = store.record_price_change_candidate(
            feed_id=feed.id,
            provider="Hivetec",
            fingerprint=canonical_manifest_fingerprint(
                feed_id=feed.id,
                provider="Hivetec",
                items=current,
            ),
            catalog_count=2,
            available_count=2,
            items=current,
        )

        def missing(
            _feed: object,
            persisted: tuple[PriceSnapshot, ...],
        ) -> tuple[CatalogObservation, ...]:
            return normalize_hivetec_catalog(
                "hivetec",
                (make_hivetec_product(1, "9000"), make_hivetec_product(3, "0")),
                persisted,
            )

        draft = create_plan(
            store,
            feed,
            batch_id=batch.batch_id,
            batch_fingerprint=batch.fingerprint,
            reason="availability defer test",
            catalog_fetch=missing,
            version=2,
        )
        plan = draft.model_copy(
            update={
                "items": tuple(
                    item.model_copy(
                        update={
                            "disposition": "defer"
                            if item.product_id in {"2", "3"}
                            else "accept",
                            "reason": "unpriced"
                            if item.product_id in {"2", "3"}
                            else "verified",
                        },
                    )
                    for item in draft.items
                ),
            },
        ).sealed()
        with DatabaseOwnership(database) as ownership:
            apply_plan(
                store,
                feed,
                plan,
                ownership,
                fingerprint=plan.fingerprint(),
                catalog_fetch=missing,
            )

        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
        catalog = HivetecCatalogStub(
            [
                (
                    make_hivetec_product(1, "9000"),
                    make_hivetec_product(2, "7500"),
                    make_hivetec_product(3, "5000"),
                ),
                (
                    make_hivetec_product(1, "9000"),
                    make_hivetec_product(2, "7500"),
                    make_hivetec_product(3, "5000"),
                ),
                (
                    make_hivetec_product(1, "9000"),
                    make_hivetec_product(2, "7500"),
                    make_hivetec_product(3, "4500"),
                ),
            ],
        )
        monitor = make_hivetec_monitor(catalog, store, sender)
        monitor.scan()
        assert sender.messages == []
        assert store.held_price_product_ids("hivetec") == frozenset()
        assert store._connection.execute(
            "SELECT previous_amount, previous_formatted, previous_currency "
            "FROM price_hold_releases WHERE product_id = '3'",
        ).fetchone() == (None, None, None)
        assert any(
            snapshot.product_id == "3" and snapshot.amount == 50
            for snapshot in store.load_price_snapshots("hivetec")
        )
        monitor.scan()
        assert sender.messages == []
        monitor.scan()
        assert len(sender.messages) == 1
