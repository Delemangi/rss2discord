"""Durable summary state transitions using local SQLite and fake senders only."""

import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.recovery_models import (
    PriceBatch,
    PriceChangeRecord,
    PriceDeliveryClaim,
    PriceSnapshot,
)
from rss2discord.transports.price_monitor.planning import prepare_price_delivery
from tests import setec_price_monitor_helpers as setec
from tests.test_adapter_c2_provider_caps import build
from tests.test_price_recovery_integration import PROVIDERS, monitor_for


def records(count: int = 101) -> tuple[PriceChangeRecord, ...]:
    return tuple(
        PriceChangeRecord(
            str(i),
            PriceSnapshot("feed", str(i), Decimal(100), "100 MKD", "MKD"),
            PriceSnapshot("feed", str(i), Decimal(90), "90 MKD", "MKD"),
        )
        for i in range(count)
    )


def stage(store: DeliveryStore, count: int = 101) -> PriceBatch:
    items = records(count)
    store.upsert_price_snapshots(item.previous for item in items)
    return store.stage_automatic_price_summary(
        feed_id="feed",
        provider="test",
        items=items,
        catalog_count=count,
        available_count=count,
    )


def test_mode_persists_in_every_loader_and_individual_paths_reject(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        batch = stage(store)
        assert batch.delivery_mode == "automatic_summary"
        assert batch.summary.approved_at is None
        active = store.load_active_price_batch("feed")
        assert active is not None
        assert active.delivery_mode == "automatic_summary"
        loaded = store.load_price_batch(batch.batch_id)
        assert loaded is not None
        assert loaded.delivery_mode == "automatic_summary"
        assert (
            store.list_price_change_batches("feed")[0].delivery_mode
            == "automatic_summary"
        )
        assert store.claim_price_delivery_attempt(batch.batch_id, "0") is None
        with pytest.raises(ValueError, match="whole-manifest"):
            store.record_approved_price_delivery(
                PriceDeliveryClaim(batch.batch_id, "0", 1),
                records()[0].current,
            )
        store.pause_price_change_batch(batch.batch_id, "operator hold")
        with pytest.raises(ValueError, match="pending approval"):
            store.approve_price_change_batch(
                feed_id="feed",
                fingerprint=batch.fingerprint,
                reason="no",
            )
    with DeliveryStore(path) as store:
        active = store.load_active_price_batch("feed")
        assert active is not None
        assert active.delivery_mode == "automatic_summary"


def test_two_connections_generation_and_crash_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = DeliveryStore(path)
    batch = stage(first)
    old = first.claim_price_summary_attempt(batch.batch_id)
    assert old is not None
    with DeliveryStore(path) as second:
        assert second.claim_price_summary_attempt(batch.batch_id) is None
        first.close()  # process-exit equivalent: release OS ownership, leave durable claim
        fresh = second.claim_price_summary_attempt(batch.batch_id)
        assert fresh is not None
        assert fresh.generation > old.generation
        assert not second.release_price_summary_attempt(old)
        with pytest.raises(ValueError, match="generation"):
            second.record_price_summary_delivery(old)
        second.record_price_summary_delivery(fresh)
        completed = second.load_price_batch(batch.batch_id)
        assert completed is not None
        assert completed.status == "completed"
        assert all(s.amount == 90 for s in second.load_price_snapshots("feed"))


@pytest.mark.parametrize(
    "mutation",
    ["amount", "formatted", "currency", "hold", "pause"],
)
def test_post_send_drift_or_operator_action_is_all_or_nothing(
    tmp_path: Path,
    mutation: str,
) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store, DeliveryStore(path) as admin:
        batch = stage(store)
        claim = store.claim_price_summary_attempt(batch.batch_id)
        assert claim is not None
        previous = records()[0].previous
        if mutation in {"amount", "formatted", "currency"}:
            admin.upsert_price_snapshot(
                replace(previous, amount=Decimal(99))
                if mutation == "amount"
                else replace(previous, formatted="changed")
                if mutation == "formatted"
                else replace(previous, currency="EUR"),
            )
        elif mutation == "hold":
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "INSERT INTO price_product_holds (feed_id, product_id, reconciliation_fingerprint, reason) VALUES ('feed','0','review','hold')",
                )
        elif mutation == "revoke":
            admin.revoke_price_change_batch(
                batch.batch_id,
                batch.fingerprint,
                "operator revoke",
            )
        else:
            admin.pause_price_change_batch(batch.batch_id, "operator pause")
        before = store.load_price_snapshots("feed")
        with pytest.raises(ValueError, match=r"baseline drift|no longer active"):
            store.record_price_summary_delivery(claim)
        assert store.load_price_snapshots("feed") == before
        loaded = store.load_price_batch(batch.batch_id)
        assert loaded is not None
        assert loaded.delivered_count == 0
        assert store.release_price_summary_attempt(claim)


def test_sql_failure_rolls_back_every_target_and_can_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        batch = stage(store)
        claim = store.claim_price_summary_attempt(batch.batch_id)
        assert claim is not None
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_summary BEFORE UPDATE ON price_snapshots WHEN OLD.product_id = '50' BEGIN SELECT RAISE(ABORT, 'fixture SQL failure'); END",
            )
        with pytest.raises(sqlite3.IntegrityError, match="fixture SQL failure"):
            store.record_price_summary_delivery(claim)
        assert all(s.amount == 100 for s in store.load_price_snapshots("feed"))
        loaded = store.load_price_batch(batch.batch_id)
        assert loaded is not None
        assert loaded.delivered_count == 0
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER fail_summary")
    with DeliveryStore(path) as store:
        fresh = store.claim_price_summary_attempt(batch.batch_id)
        assert fresh is not None
        store.record_price_summary_delivery(fresh)
        assert all(s.amount == 90 for s in store.load_price_snapshots("feed"))


def test_new_summary_preserves_legacy_history_and_replaces_only_fresh_candidate(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        items = records()
        legacy = store.record_price_change_candidate(
            feed_id="feed",
            provider="test",
            items=items,
            catalog_count=101,
            available_count=101,
            fingerprint=canonical_manifest_fingerprint(
                feed_id="feed",
                provider="test",
                items=items,
            ),
        )
        batch = stage(store)
        old = store.load_price_batch(legacy.batch_id)
        assert old is not None
        assert old.status == "revoked"
        assert old.delivery_mode == "individual"
        assert old.items == legacy.items
        assert batch.summary.approved_at is None


@pytest.mark.parametrize("count", [100, 101])
def test_planner_boundary(tmp_path: Path, count: int) -> None:
    items = records(count)
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshots(item.previous for item in items)
        plan = prepare_price_delivery(
            store=store,
            feed_id="feed",
            provider="test",
            changes=items,
            current={i.product_id: i.current for i in items},
            persisted={i.product_id: i.previous for i in items},
            catalog_count=count,
        )
        assert (plan.summary is not None) == (count == 101)
        assert len(plan.selected_ids) == (10 if count == 100 else 0)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_all_monitors_deliver_automatic_summary(tmp_path: Path, provider: str) -> None:
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = (
            monitor_for(provider, [(100,) * 101, (90,) * 101], store, sender)
            if provider in {"ddstore", "hivetec"}
            else build(provider, 101, store, sender)
        )
        monitor.scan()
        monitor.scan()
        batches = store.list_price_change_batches(provider, include_terminal=True)
        assert len(sender.messages) == 1
        assert batches[0].delivery_mode == "automatic_summary"
        assert batches[0].status == "completed"
        assert batches[0].delivered_count == (10 if provider == "cccenter" else 101)
        message = sender.messages[0]
        assert message.entry.title == "Price change summary"
        assert len(message.entry.description) < 600
        assert message.entry.image_url is None


@pytest.mark.parametrize(
    "result",
    [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.INTERRUPTED],
)
def test_failed_summary_retries_historical_manifest_without_confirmation(
    tmp_path: Path,
    result: DiscordDeliveryResult,
) -> None:
    baseline = tuple(
        setec.make_product(f"p-{i:03}", calculated_amount=100) for i in range(201)
    )
    target = tuple(setec.make_product(p.id, calculated_amount=90) for p in baseline)
    moved = tuple(setec.make_product(p.id, calculated_amount=80) for p in baseline)
    catalog = setec.CatalogStub([baseline, target, moved])
    sender = setec.RecordingSender([result, DiscordDeliveryResult.DELIVERED])
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()
        batch = store.load_active_price_batch("setec")
        assert batch is not None
        assert batch.item_count == 200
        assert all(s.amount == 100 for s in store.load_price_snapshots("setec"))
        assert store.list_health("setec")[0].state == "failed"
        assert store.list_health("setec")[0].cause == "AutomaticSummaryRetryPending"
    with DeliveryStore(path) as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        assert len(catalog.requested_id_batches) == 1
        assert len(catalog.requested_id_batches[0]) == 200
        snapshots = store.load_price_snapshots("setec")
        assert sum(s.amount == 90 for s in snapshots) == 200
        assert sum(s.amount == 100 for s in snapshots) == 1
        assert (
            sender.messages[0].entry.description.split("\n")[:3]
            == sender.messages[1].entry.description.split("\n")[:3]
        )
