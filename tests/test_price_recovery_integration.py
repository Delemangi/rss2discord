"""Real monitor-to-store recovery regressions for DDStore and Hivetec."""

from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from time import time

import pytest

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_safety import MAX_PRICE_MANIFEST_ITEMS
from rss2discord.recovery_models import HealthUpdate, PriceChangeRecord
from rss2discord.transports.ddstore_price_monitor import DDStorePriceMonitor
from rss2discord.transports.hivetec_price_monitor import HivetecPriceMonitor
from tests import test_ddstore_price_monitor as dd
from tests import test_hivetec_price_monitor as hive
from tests.setec_price_monitor_helpers import RecordingSender


def monitor_for(
    provider: str,
    batches: list[tuple[int, ...]],
    store: DeliveryStore,
    sender: RecordingSender,
) -> DDStorePriceMonitor | HivetecPriceMonitor:
    if provider == "ddstore":
        catalog = dd.CatalogStub(
            [
                tuple(
                    dd.make_product(str(i), amount=price)
                    for i, price in enumerate(batch, 1)
                )
                for batch in batches
            ],
        )
        return dd.make_monitor(dd.make_feed(), catalog, store, sender)
    catalog_h = hive.CatalogStub(
        [
            tuple(hive.product(i, str(price * 100)) for i, price in enumerate(batch, 1))
            for batch in batches
        ],
    )
    return hive.monitor(catalog_h, store, sender)


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
@pytest.mark.parametrize("count", [1, 100])
def test_normal_changes_send_without_approval_with_ten_attempt_cap(
    tmp_path: Path,
    provider: str,
    count: int,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED] * 10)
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = monitor_for(provider, [(100,) * count, (90,) * count], store, sender)
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == min(count, 10)
        assert store.list_price_change_batches(provider) == ()
        assert sum(p.amount == 90 for p in store.load_price_snapshots(provider)) == min(
            count,
            10,
        )
        assert store.list_health(provider)[0].state == "healthy"


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
def test_actual_101_changes_quarantine_then_drain_same_approval(
    tmp_path: Path,
    provider: str,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED] * 101)
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = monitor_for(
            provider,
            [(100,) * 101, *([(90,) * 101] * 12)],
            store,
            sender,
        )
        monitor.scan()
        before = store.load_price_snapshots(provider)
        monitor.scan()
        assert store.load_price_snapshots(provider) == before
        assert sender.messages == []
        candidate = store.list_price_change_batches(provider)[0]
        assert candidate.status == "candidate"
        assert candidate.pending_count == 101
        assert store.list_health(provider)[0].state == "quarantined"
        store.approve_price_change_batch(
            feed_id=provider,
            fingerprint=candidate.fingerprint,
            reason="reviewed all 101 changes",
        )
        monitor.scan()
        active = store.load_active_price_batch(provider)
        assert active is not None
        assert active.fingerprint == candidate.fingerprint
        assert active.pending_count == 91
        assert active.delivered_count == 10
        assert sum(item.attempt_count for item in active.items) == 10
        assert len(sender.messages) == 10
        monitor.scan()
        shrunk = store.load_active_price_batch(provider)
        assert shrunk is not None
        assert shrunk.fingerprint == candidate.fingerprint
        assert shrunk.pending_count == 81
        for _ in range(9):
            monitor.scan()
        assert store.load_active_price_batch(provider) is None
        assert len(sender.messages) == 101
        assert all(p.amount == 90 for p in store.load_price_snapshots(provider))
        assert store.list_health(provider)[0].state == "healthy"


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
def test_unapproved_movement_replaces_full_candidate_without_writes(
    tmp_path: Path,
    provider: str,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        sender = RecordingSender([])
        monitor = monitor_for(
            provider,
            [(100,) * 101, (90,) * 101, (80,) * 101],
            store,
            sender,
        )
        monitor.scan()
        before = store.load_price_snapshots(provider)
        monitor.scan()
        old = store.list_price_change_batches(provider)[0]
        monitor.scan()
        candidates = store.list_price_change_batches(provider)
        assert len(candidates) == 1
        assert candidates[0].fingerprint != old.fingerprint
        assert candidates[0].pending_count == 101
        assert candidates[0].status == "candidate"
        assert store.load_price_snapshots(provider) == before
        assert sender.messages == []


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
@pytest.mark.parametrize(
    "failure",
    ["source", "previous", "missing", "unavailable", "fetch", "challenge"],
)
def test_approved_invalid_manifest_or_fetch_pauses_without_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    failure: str,
) -> None:
    target = (90,) * 101
    live: tuple[int, ...] = target
    if failure == "source":
        live = (*target[:-1], 80)
    if failure == "missing":
        live = target[:-1]
    if failure == "unavailable":
        live = (*target[:-1], 0)
    with DeliveryStore(tmp_path / "state.db") as store:
        sender = RecordingSender([])
        monitor = monitor_for(
            provider,
            [(100,) * 101, target, live, target],
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        candidate = store.list_price_change_batches(provider)[0]
        store.approve_price_change_batch(
            feed_id=provider,
            fingerprint=candidate.fingerprint,
            reason="reviewed",
        )
        if failure == "previous":
            store.upsert_price_snapshot(
                replace(store.load_price_snapshots(provider)[-1], amount=Decimal(99)),
            )
        if failure in {"fetch", "challenge"}:

            def fail(*args: object, **kwargs: object) -> None:
                raise FeedFetchError(
                    provider,
                    "BotChallenge" if failure == "challenge" else "IncompleteCatalog",
                )

            monkeypatch.setattr(monitor._dependencies.catalog, "fetch_catalog", fail)
        monitor.scan()
        active = store.load_active_price_batch(provider)
        assert active is not None
        assert active.status == "paused"
        assert active.pending_count == 101
        assert sender.messages == []
        assert store.list_health(provider)[0].state == (
            "blocked" if failure == "challenge" else "recovery_required"
        )
        if failure == "challenge":
            assert store.get_blocked_until(provider) is not None
        if failure not in {"fetch", "challenge"}:
            monitor.scan()
            assert store.load_active_price_batch(provider) is not None
            assert sender.messages == []


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
def test_failed_approved_items_rotate_beyond_ten_lifetime_attempts(
    tmp_path: Path,
    provider: str,
) -> None:
    scans = 122
    sender = RecordingSender([DiscordDeliveryResult.FAILED] * (10 * scans))
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = monitor_for(
            provider,
            [(100,) * 101, *([(90,) * 101] * (scans + 1))],
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        candidate = store.list_price_change_batches(provider)[0]
        store.approve_price_change_batch(
            feed_id=provider,
            fingerprint=candidate.fingerprint,
            reason="reviewed",
        )
        for _ in range(scans):
            prior_count = len(sender.messages)
            monitor.scan()
            assert 1 <= len(sender.messages) - prior_count <= 10
        active = store.load_active_price_batch(provider)
        assert active is not None
        assert active.status == "approved"
        attempts = [item.attempt_count for item in active.items]
        assert min(attempts) > 10
        assert max(attempts) - min(attempts) <= 1
        assert active.pending_count == 101
        assert all(p.amount == 100 for p in store.load_price_snapshots(provider))


def test_repeated_health_quarantine_transition_updates_without_binding_error(
    tmp_path: Path,
) -> None:
    now = int(time())
    with DeliveryStore(tmp_path / "state.db") as store:
        first = store.record_health(
            HealthUpdate(
                feed_id="feed",
                job_kind="price",
                state="quarantined",
                cause="PriceChangeApprovalRequired",
                attempted_at=now,
                success=False,
                nonempty=False,
                item_count=0,
            ),
        )
        second = store.record_health(
            HealthUpdate(
                feed_id="feed",
                job_kind="price",
                state="quarantined",
                cause="SourceMoved",
                attempted_at=now + 1,
                success=False,
                nonempty=False,
                item_count=0,
            ),
        )
        assert first.changed
        assert not second.changed
        health = store.list_health("feed")[0]
        assert health.cause == "SourceMoved"
        assert health.total_attempts == 2


def test_oversized_manifest_is_rejected_before_database_write(tmp_path: Path) -> None:
    def records() -> Iterator[PriceChangeRecord]:
        for index in range(MAX_PRICE_MANIFEST_ITEMS + 1):
            product_id = str(index)
            yield PriceChangeRecord(
                product_id,
                PriceSnapshot("feed", product_id, Decimal(1), "1 EUR", "EUR"),
                PriceSnapshot("feed", product_id, Decimal(2), "2 EUR", "EUR"),
            )

    with DeliveryStore(tmp_path / "state.db") as store:
        with pytest.raises(ValueError, match="manifest item limit"):
            store.record_price_change_candidate(
                feed_id="feed",
                provider="test",
                fingerprint="not-computed",
                catalog_count=MAX_PRICE_MANIFEST_ITEMS + 1,
                available_count=MAX_PRICE_MANIFEST_ITEMS + 1,
                items=records(),
            )
        assert store.list_price_change_batches("feed", include_terminal=True) == ()
