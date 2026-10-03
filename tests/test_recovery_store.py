from decimal import Decimal
from pathlib import Path
from time import time

import pytest

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.recovery_models import HealthUpdate, PriceChangeRecord


def _change(feed_id: str, product_id: str, old: str, new: str) -> PriceChangeRecord:
    return PriceChangeRecord(
        product_id,
        PriceSnapshot(feed_id, product_id, Decimal(old), f"{old} EUR", "EUR"),
        PriceSnapshot(feed_id, product_id, Decimal(new), f"{new} EUR", "EUR"),
    )


def _candidate(store: DeliveryStore, feed_id: str = "feed") -> int:
    records = (_change(feed_id, "one", "1", "2"), _change(feed_id, "two", "3", "4"))
    fingerprint = canonical_manifest_fingerprint(
        feed_id=feed_id,
        provider="provider",
        items=records,
    )
    store.record_price_change_candidate(
        feed_id=feed_id,
        provider="provider",
        fingerprint=fingerprint,
        catalog_count=2,
        available_count=2,
        items=records,
    )
    return store.approve_price_change_batch(
        feed_id=feed_id,
        fingerprint=fingerprint,
        reason="operator reviewed source movement",
    ).batch_id


def test_price_manifest_approval_and_atomic_delivery(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        batch_id = _candidate(store)
        first = store.begin_price_delivery_attempt(batch_id)
        second = store.begin_price_delivery_attempt(batch_id)

        assert first is not None
        assert first.product_id == "one"
        assert second is not None
        assert second.product_id == "two"
        assert store.complete_price_change_batch_if_drained(batch_id) is False

        store.record_approved_price_delivery(batch_id, "one", first.current)
        assert store.load_price_snapshots("feed") == (first.current,)
        assert store.complete_price_change_batch_if_drained(batch_id) is False
        store.record_approved_price_delivery(batch_id, "two", second.current)

        assert store.complete_price_change_batch_if_drained(batch_id)
        assert store.load_price_batch(batch_id).status == "completed"  # type: ignore[union-attr]


def test_delivery_requires_reservation_and_rejects_duplicate_without_mutation(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        batch_id = _candidate(store)
        batch = store.load_price_batch(batch_id)
        assert batch is not None
        first = batch.items[0]
        with pytest.raises(ValueError, match="not reserved"):
            store.record_approved_price_delivery(
                batch_id,
                first.product_id,
                first.current,
            )
        assert store.load_price_snapshots("feed") == ()
        assert store.load_price_batch(batch_id).items[0].status == "pending"  # type: ignore[union-attr]

        reserved = store.begin_price_delivery_attempt(batch_id)
        assert reserved is not None
        store.record_approved_price_delivery(
            batch_id,
            reserved.product_id,
            reserved.current,
        )
        snapshot_after_delivery = store.load_price_snapshots("feed")
        item_after_delivery = store.load_price_batch(batch_id).items[0]  # type: ignore[union-attr]
        with pytest.raises(ValueError, match="already completed"):
            store.record_approved_price_delivery(
                batch_id,
                reserved.product_id,
                reserved.current,
            )

        assert store.load_price_snapshots("feed") == snapshot_after_delivery
        assert store.load_price_batch(batch_id).items[0] == item_after_delivery  # type: ignore[union-attr]
        assert store.load_price_batch(batch_id).items[1].status == "pending"  # type: ignore[union-attr]


def test_delivery_target_mismatch_rolls_back_conditional_item_update(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        batch_id = _candidate(store)
        reserved = store.begin_price_delivery_attempt(batch_id)
        assert reserved is not None
        wrong = PriceSnapshot(
            reserved.current.feed_id,
            reserved.current.product_id,
            Decimal(999),
            "999 EUR",
            "EUR",
        )
        with pytest.raises(ValueError, match="does not match"):
            store.record_approved_price_delivery(batch_id, reserved.product_id, wrong)
        item = store.load_price_batch(batch_id).items[0]  # type: ignore[union-attr]
        assert item.status == "pending"
        assert item.attempt_count == 1
        assert store.load_price_snapshots("feed") == ()


def test_price_candidate_requires_exact_manifest_and_active_uniqueness(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        records = (_change("feed", "one", "1", "2"),)
        with pytest.raises(ValueError, match="fingerprint"):
            store.record_price_change_candidate(
                feed_id="feed",
                provider="provider",
                fingerprint="wrong",
                catalog_count=1,
                available_count=1,
                items=records,
            )
        first = _candidate(store)
        other_records = (_change("feed", "other", "5", "6"),)
        other_fingerprint = canonical_manifest_fingerprint(
            feed_id="feed",
            provider="provider",
            items=other_records,
        )
        store.record_price_change_candidate(
            feed_id="feed",
            provider="provider",
            fingerprint=other_fingerprint,
            catalog_count=1,
            available_count=1,
            items=other_records,
        )
        with pytest.raises(ValueError, match="active"):
            store.approve_price_change_batch(
                feed_id="feed",
                fingerprint=other_fingerprint,
                reason="second approval",
            )
        assert store.load_active_price_batch("feed").batch_id == first  # type: ignore[union-attr]


def test_baseline_is_separate_from_legacy_deliveries(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.mark_delivered("legacy", "already-delivered")
        candidate = store.record_feed_baseline_candidate(
            feed_id="legacy",
            entry_ids=("new-1", "new-2"),
            reason="validated complete inventory",
        )
        store.approve_feed_baseline_candidate(
            feed_id="legacy",
            fingerprint=candidate.fingerprint,
            reason="recovery review",
        )

        assert store.has_complete_baseline("legacy")
        assert store.has_baselined("legacy", "new-1")
        assert store.has_handled_entry("legacy", "new-1")
        assert not store.has_delivered("legacy", "new-1")
        assert store.count_delivered("legacy") == 1


def test_health_block_cooldown_is_shared_and_timing_preserves_state(
    tmp_path: Path,
) -> None:
    now = int(time())
    with DeliveryStore(tmp_path / "state.db") as store:
        first = store.record_health(
            HealthUpdate(
                "feed",
                "ordinary",
                "blocked",
                "challenge",
                now,
                False,
                False,
                0,
            ),
        )
        blocked_until = store.get_blocked_until("feed")
        store.record_job_timing("feed", "ordinary", 123, 45)
        second = store.record_health(
            HealthUpdate(
                "feed",
                "price",
                "failed",
                "cooldown",
                now + 1,
                False,
                False,
                0,
            ),
        )

        assert first.changed
        assert first.should_log
        assert second.changed
        assert blocked_until == now + 21600
        assert store.get_blocked_until("feed") == blocked_until
        assert store.list_health("feed")[0].state == "blocked"
        assert store.list_health("feed")[0].duration_ms == 123
