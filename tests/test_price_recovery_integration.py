"""Real monitor-to-store recovery regressions for DDStore and Hivetec."""

from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from time import time

import pytest

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import (
    DiscordDeliveryResult,
    SleepCallback,
    WebhookMessage,
)
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_safety import MAX_PRICE_MANIFEST_ITEMS
from rss2discord.providers.ddstore.prices import DDStorePriceMonitor
from rss2discord.recovery_models import (
    HealthUpdate,
    PriceChangeRecord,
    PriceDeliveryClaim,
)
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.hivetec_price_monitor import HivetecPriceMonitor
from tests import test_ddstore_price_monitor as dd
from tests import test_hivetec_price_monitor as hive
from tests.setec_price_monitor_helpers import RecordingSender
from tests.test_adapter_c2_provider_caps import Monitor, build

PROVIDERS = (
    "anhoch",
    "neksio",
    "setec",
    "cccenter",
    "gjirafa50",
    "neptun",
    "pazar3",
    "reklama5",
    "technomarket",
    "ddstore",
    "hivetec",
)


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
@pytest.mark.parametrize("checkpoint", [1, 2, 3])
def test_catalog_shutdown_before_planning_never_writes_or_sends(
    tmp_path: Path,
    provider: str,
    checkpoint: int,
) -> None:
    sender = RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = monitor_for(provider, [(100,)], store, sender)
        checks = iter([False] * (checkpoint - 1) + [True])
        monitor._dependencies = replace(
            monitor._dependencies,
            delivery=replace(
                monitor._dependencies.delivery,
                is_shutdown_requested=lambda: next(checks),
            ),
        )
        with pytest.raises(FeedFetchInterruptedError):
            monitor.scan()
        assert store.load_price_snapshots(provider) == ()
        assert store.list_price_change_batches(provider) == ()
        assert store.list_health(provider) == ()
        assert sender.messages == []


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
def test_catalog_silent_updates_wait_until_approved_batch_finishes(
    tmp_path: Path,
    provider: str,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED] * 101)
    with DeliveryStore(tmp_path / "state.db") as store:
        changed = (90,) * 101 + (100,)
        live = (*changed, 70)
        monitor = monitor_for(
            provider,
            [(100,) * 102, changed, *([live] * 12)],
            store,
            sender,
        )
        monitor.scan()
        unchanged = next(
            snapshot
            for snapshot in store.load_price_snapshots(provider)
            if snapshot.product_id == "102"
        )
        stale = replace(unchanged, formatted="stale display")
        store.upsert_price_snapshot(stale)
        monitor.scan()
        candidate = store.list_price_change_batches(provider)[0]
        store.approve_price_change_batch(
            feed_id=provider,
            fingerprint=candidate.fingerprint,
            reason="fixture review",
        )
        for _ in range(11):
            monitor.scan()
        snapshots = {s.product_id: s for s in store.load_price_snapshots(provider)}
        assert len(sender.messages) == 101
        assert store.load_active_price_batch(provider) is None
        assert snapshots["102"] == stale
        assert "103" not in snapshots
        monitor.scan()
        snapshots = {s.product_id: s for s in store.load_price_snapshots(provider)}
        assert snapshots["102"] == unchanged
        assert snapshots["103"].amount == 70
        assert len(sender.messages) == 101


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


@pytest.mark.parametrize("provider", PROVIDERS)
def test_inflight_success_is_acknowledged_after_revoke_and_next_send_is_blocked(
    tmp_path: Path,
    provider: str,
) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store, DeliveryStore(path) as admin:

        class RevokingSender(RecordingSender):
            def send(
                self,
                message: WebhookMessage,
                sleep: SleepCallback,
            ) -> DiscordDeliveryResult:
                active = admin.load_active_price_batch(provider)
                assert active is not None
                # Exactly this send has a claim; later selected IDs are not reserved.
                assert sum(item.attempt_count for item in active.items) == 1
                admin.revoke_price_change_batch(
                    active.batch_id,
                    active.fingerprint,
                    "revoke during send",
                )
                return super().send(message, sleep)

        sender = RevokingSender([DiscordDeliveryResult.DELIVERED])
        monitor: Monitor = (
            monitor_for(
                provider,
                [(100,) * 101, (90,) * 101, (90,) * 101],
                store,
                sender,
            )
            if provider in {"ddstore", "hivetec"}
            else build(provider, 101, store, sender)
        )
        monitor.scan()
        monitor.scan()
        candidate = store.list_price_change_batches(provider)[0]
        store.approve_price_change_batch(
            feed_id=provider,
            fingerprint=candidate.fingerprint,
            reason="reviewed",
        )
        monitor.scan()
        batch = store.load_price_batch(candidate.batch_id)
        assert batch is not None
        assert batch.status == "revoked"
        assert batch.delivered_count == 1
        assert batch.pending_count == 100
        assert sum(item.attempt_count for item in batch.items) == 1
        assert len(sender.messages) == 1
        assert sum(s.amount == 90 for s in store.load_price_snapshots(provider)) == 1


@pytest.mark.parametrize(
    "outcome",
    [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.INTERRUPTED, "exception"],
)
def test_unsuccessful_sender_releases_exact_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: DiscordDeliveryResult | str,
) -> None:
    class UnsuccessfulSender(RecordingSender):
        def send(
            self,
            message: WebhookMessage,
            sleep: SleepCallback,
        ) -> DiscordDeliveryResult:
            if outcome == "exception":
                raise RuntimeError("fixture sender failed")
            return super().send(message, sleep)

    sender = UnsuccessfulSender(
        [outcome] * 10 if isinstance(outcome, DiscordDeliveryResult) else [],
    )
    with DeliveryStore(tmp_path / "state.db") as store:
        claims: list[PriceDeliveryClaim] = []
        claim_attempt = store.claim_price_delivery_attempt

        def record_claim(batch_id: int, product_id: str) -> PriceDeliveryClaim | None:
            claim = claim_attempt(batch_id, product_id)
            if claim is not None:
                claims.append(claim)
            return claim

        monkeypatch.setattr(store, "claim_price_delivery_attempt", record_claim)
        monitor = monitor_for(
            "ddstore",
            [(100,) * 101, (90,) * 101, (90,) * 101],
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        candidate = store.list_price_change_batches("ddstore")[0]
        store.approve_price_change_batch(
            feed_id="ddstore",
            fingerprint=candidate.fingerprint,
            reason="reviewed",
        )
        if outcome == "exception":
            with pytest.raises(RuntimeError, match="fixture sender failed"):
                monitor.scan()
        else:
            monitor.scan()
        batch = store.load_price_batch(candidate.batch_id)
        assert batch is not None
        assert claims
        assert all(not store.release_price_delivery_attempt(claim) for claim in claims)
        assert sum(item.attempt_count for item in batch.items) == (
            10 if outcome is DiscordDeliveryResult.FAILED else 1
        )
        assert batch.delivered_count == 0
        assert all(s.amount == 100 for s in store.load_price_snapshots("ddstore"))
