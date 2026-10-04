from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.database_ownership import DatabaseOwnership
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.reconciliation import apply_plan, create_plan
from rss2discord.recovery_models import (
    PriceChangeRecord,
    PriceDeliveryClaim,
    PriceSnapshot,
)
from rss2discord.transports.catalog_normalization import (
    normalize_ddstore_catalog,
    normalize_hivetec_catalog,
)
from rss2discord.transports.price_monitor import prepare_price_delivery
from tests.reconciliation_helpers import fetch_current, reviewed, setup_plan
from tests.setec_price_monitor_helpers import RecordingSender
from tests.test_ddstore_price_monitor import (
    CatalogStub,
    make_feed,
    make_monitor,
    make_product,
)
from tests.test_hivetec_price_monitor import CatalogStub as HivetecStub
from tests.test_hivetec_price_monitor import feed as hivetec_feed
from tests.test_hivetec_price_monitor import monitor as hivetec_monitor
from tests.test_hivetec_price_monitor import product as hivetec_product


def test_ddstore_real_monitor_future_only_and_permanent_holds(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    feed = make_feed()
    baseline = tuple(make_product(str(i), amount=100) for i in range(101))
    current = tuple(
        make_product(str(i), amount=Decimal("1.05") if i == 0 else 90)
        for i in range(101)
    )
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED] * 110)
    with DeliveryStore(database) as store:
        monitor = make_monitor(feed, CatalogStub([baseline, current]), store, sender)
        monitor.scan()
        monitor.scan()
        assert sender.messages == []
        batch = store.list_price_change_batches(feed.id)[0]
        with DatabaseOwnership(database) as ownership:
            draft = create_plan(
                store,
                feed,
                batch_id=batch.batch_id,
                batch_fingerprint=batch.fingerprint,
                reason="review all current prices",
                catalog_fetch=lambda _feed, old: normalize_ddstore_catalog(
                    feed.id,
                    current,
                    old,
                ),
            )
            plan = draft.model_copy(
                update={
                    "items": tuple(
                        item.model_copy(
                            update={
                                "disposition": "hold"
                                if item.product_id == "0"
                                else "accept",
                            },
                        )
                        for item in draft.items
                    ),
                },
            ).sealed()
            apply_plan(
                store,
                feed,
                plan,
                ownership,
                fingerprint=plan.fingerprint(),
                catalog_fetch=lambda _feed, old: normalize_ddstore_catalog(
                    feed.id,
                    current,
                    old,
                ),
            )
        make_monitor(feed, CatalogStub([current]), store, sender).scan()
        assert sender.messages == []
        assert store.list_price_change_batches(feed.id) == ()
    # Hold persists across restart, disappearance, reappearance, and a catalog
    # below the quarantine threshold. The accepted product's *future* move is
    # the only notification, and its previous amount is the reconciled 90.
    with DeliveryStore(database) as store:
        disappeared = tuple(product for product in current if product.uid != "0")
        future = tuple(
            make_product(
                str(i),
                amount=80 if i == 1 else 90,
                stock_status="OUT_OF_STOCK",
            )
            for i in range(101)
        )
        make_monitor(
            feed,
            CatalogStub([disappeared, future, future]),
            store,
            sender,
        ).scan()
        make_monitor(feed, CatalogStub([future]), store, sender).scan()
        assert len(sender.messages) == 1
        assert sender.messages[0].entry.link.endswith("product-1.html")
        assert sender.messages[0].entry.source_metrics[1].value == "90 ден."
        snapshots = {
            item.product_id: item.amount for item in store.load_price_snapshots(feed.id)
        }
        assert snapshots["0"] == Decimal(100)
        assert snapshots["1"] == Decimal(80)
        assert store.held_price_product_ids(feed.id) == {"0"}
        hundred_actionable = tuple(make_product(str(i), amount=70) for i in range(101))
        make_monitor(feed, CatalogStub([hundred_actionable]), store, sender).scan()
        assert len(sender.messages) == 11
        assert store.list_price_change_batches(feed.id) == ()
        assert {
            item.product_id: item.amount for item in store.load_price_snapshots(feed.id)
        }["0"] == Decimal(100)


def test_hivetec_real_monitor_future_only_after_reconciliation(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    feed = hivetec_feed()
    baseline = tuple(hivetec_product(i, "149900") for i in range(1, 102))
    current = tuple(hivetec_product(i, "129900") for i in range(1, 102))
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED] * 10)
    with DeliveryStore(database) as store:
        monitor = hivetec_monitor(HivetecStub([baseline, current]), store, sender)
        monitor.scan()
        monitor.scan()
        assert sender.messages == []
        batch = store.list_price_change_batches(feed.id)[0]
        with DatabaseOwnership(database) as ownership:
            draft = create_plan(
                store,
                feed,
                batch_id=batch.batch_id,
                batch_fingerprint=batch.fingerprint,
                reason="explicit full-catalog review",
                catalog_fetch=lambda _feed, old: normalize_hivetec_catalog(
                    feed.id,
                    current,
                    old,
                ),
            )
            plan = draft.model_copy(
                update={
                    "items": tuple(
                        item.model_copy(
                            update={
                                "disposition": "hold"
                                if item.product_id == "2"
                                else "accept",
                            },
                        )
                        for item in draft.items
                    ),
                },
            ).sealed()
            apply_plan(
                store,
                feed,
                plan,
                ownership,
                fingerprint=plan.fingerprint(),
                catalog_fetch=lambda _feed, old: normalize_hivetec_catalog(
                    feed.id,
                    current,
                    old,
                ),
            )
        future = tuple(
            hivetec_product(i, "119900" if i in {1, 2} else "129900")
            for i in range(1, 102)
        )
        monitor = hivetec_monitor(HivetecStub([current, future]), store, sender)
        monitor.scan()
        assert sender.messages == []
        monitor.scan()
        assert len(sender.messages) == 1
        assert sender.messages[0].entry.link.endswith("product-1/")
        assert sender.messages[0].entry.source_metrics[1].value == "1.299 ден."
        assert {
            item.product_id: item.amount for item in store.load_price_snapshots(feed.id)
        }["2"] == Decimal(1499)


def test_store_guards_hold_silent_normal_manifest_approved_claim_and_ack(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        original = store.load_price_batch(plan.batch_id)
        assert original is not None
        apply_plan(
            store,
            make_feed(),
            plan,
            ownership,
            fingerprint=plan.fingerprint(),
            catalog_fetch=fetch_current,
        )
        held = PriceSnapshot("ddstore", "2", Decimal(7), "7 EUR", "EUR")
        store.upsert_price_snapshot(held)
        assert {
            item.product_id: item.amount
            for item in store.load_price_snapshots("ddstore")
        }["2"] == Decimal(100)
        assert store.select_normal_price_deliveries(
            feed_id="ddstore",
            product_ids=("1", "2"),
        ) == ("1",)
        item = original.items[1]
        change = PriceChangeRecord("2", item.previous, item.current)
        with pytest.raises(ValueError, match="held"):
            store.record_price_change_candidate(
                feed_id="ddstore",
                provider="DDStore",
                fingerprint=canonical_manifest_fingerprint(
                    feed_id="ddstore",
                    provider="DDStore",
                    items=(change,),
                ),
                catalog_count=2,
                available_count=2,
                items=(change,),
            )
        # Simulate an old retained approved manifest defensively: store methods
        # must still refuse a held product even if callers ignore the monitor.
        store._connection.execute(
            "UPDATE price_change_batches SET status = 'candidate' WHERE batch_id = ?",
            (plan.batch_id,),
        )
        store._connection.commit()
        with pytest.raises(ValueError, match="held"):
            store.approve_price_change_batch(
                feed_id="ddstore",
                fingerprint=plan.batch_fingerprint,
                reason="cannot release via approval",
            )
        store._connection.execute(
            "UPDATE price_change_batches SET status = 'approved' WHERE batch_id = ?",
            (plan.batch_id,),
        )
        store._connection.commit()
        assert store.claim_price_delivery_attempt(plan.batch_id, "2") is None
        store._connection.execute(
            "UPDATE price_change_batch_items SET attempt_count = 1, claim_generation = 1, claim_open = 1 WHERE batch_id = ? AND product_id = '2'",
            (plan.batch_id,),
        )
        store._connection.commit()
        with pytest.raises(ValueError, match="held"):
            store.record_approved_price_delivery(
                PriceDeliveryClaim(plan.batch_id, "2", 1),
                item.current,
            )


def test_holds_excluded_before_currency_comparison_and_difference_counts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        apply_plan(
            store,
            make_feed(),
            plan,
            ownership,
            fingerprint=plan.fingerprint(),
            catalog_fetch=fetch_current,
        )
        previous = {
            snapshot.product_id: snapshot
            for snapshot in store.load_price_snapshots("ddstore")
        }
        different_currency = PriceSnapshot(
            "ddstore",
            "2",
            Decimal(999),
            "999 EUR",
            "EUR",
        )
        decision = prepare_price_delivery(
            store=store,
            feed_id="ddstore",
            provider="DDStore",
            current={"2": different_currency},
            persisted=previous,
            changes=(PriceChangeRecord("2", previous["2"], different_currency),),
            catalog_count=1,
        )
        assert decision.selected_ids == ()
        assert not decision.blocked
        assert store.held_price_product_ids("ddstore") == {"2"}


def test_pinned_original_manifest_survives_more_than_ten_terminal_batches(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        apply_plan(
            store,
            make_feed(),
            plan,
            ownership,
            fingerprint=plan.fingerprint(),
            catalog_fetch=fetch_current,
        )
        original = store.load_price_batch(plan.batch_id)
        for index in range(15):
            previous = PriceSnapshot("ddstore", "1", Decimal(index + 10), "old", "MKD")
            current = PriceSnapshot("ddstore", "1", Decimal(index + 11), "new", "MKD")
            change = PriceChangeRecord("1", previous, current)
            fingerprint = canonical_manifest_fingerprint(
                feed_id="ddstore",
                provider="DDStore",
                items=(change,),
            )
            batch = store.record_price_change_candidate(
                feed_id="ddstore",
                provider="DDStore",
                fingerprint=fingerprint,
                catalog_count=2,
                available_count=2,
                items=(change,),
            )
            store.revoke_price_change_batch(
                batch_id=batch.batch_id,
                fingerprint=fingerprint,
                reason="retire test",
            )
        assert store.load_price_batch(plan.batch_id) == original
        assert (
            len(store.list_price_change_batches("ddstore", include_terminal=True)) == 11
        )
        assert store.load_price_reconciliation(plan.fingerprint()) is not None
        assert store.held_price_product_ids("ddstore") == {"2"}
