from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.recovery_models import (
    PriceChangeRecord,
    PriceDeliveryClaim,
    PriceSnapshot,
)


def _change(product_id: str = "p") -> PriceChangeRecord:
    return PriceChangeRecord(
        product_id,
        PriceSnapshot("feed", product_id, Decimal(1), "1 EUR", "EUR"),
        PriceSnapshot("feed", product_id, Decimal(2), "2 EUR", "EUR"),
    )


def _approved(store: DeliveryStore, product_id: str = "p") -> int:
    item = _change(product_id)
    fingerprint = canonical_manifest_fingerprint(
        feed_id="feed",
        provider="test",
        items=(item,),
    )
    store.record_price_change_candidate(
        feed_id="feed",
        provider="test",
        fingerprint=fingerprint,
        catalog_count=1,
        available_count=1,
        items=(item,),
    )
    return store.approve_price_change_batch(
        feed_id="feed",
        fingerprint=fingerprint,
        reason="reviewed",
    ).batch_id


def test_revoke_then_late_ack_is_allowed_but_new_claim_is_blocked(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    first = DeliveryStore(database)
    second = DeliveryStore(database)
    try:
        batch_id = _approved(first)
        claim = first.claim_price_delivery_attempt(batch_id, "p")
        assert claim == PriceDeliveryClaim(batch_id, "p", 1)
        second.revoke_price_change_batch(
            batch_id=batch_id,
            fingerprint=first.load_price_batch(batch_id).fingerprint,  # type: ignore[union-attr]
            reason="withdraw while send is in flight",
        )
        assert second.claim_price_delivery_attempt(batch_id, "p") is None
        assert claim is not None
        first.record_approved_price_delivery(claim, _change().current)
        assert first.load_price_snapshots("feed")[0].product_id == "p"
    finally:
        second.close()
        first.close()


def test_revoke_before_claim_and_stale_generation_do_not_mutate(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        batch_id = _approved(store)
        fingerprint = store.load_price_batch(batch_id).fingerprint  # type: ignore[union-attr]
        store.revoke_price_change_batch(
            batch_id=batch_id,
            fingerprint=fingerprint,
            reason="obsolete",
        )
        assert store.claim_price_delivery_attempt(batch_id, "p") is None

    with DeliveryStore(database) as store:
        batch_id = _approved(store, "q")
        first = store.claim_price_delivery_attempt(batch_id, "q")
        second = store.claim_price_delivery_attempt(batch_id, "q")
        assert first is not None
        assert second is not None
        with pytest.raises(ValueError, match="not reserved"):
            store.record_approved_price_delivery(first, _change("q").current)
        assert store.load_price_snapshots("feed") == ()
        store.record_approved_price_delivery(second, _change("q").current)


def test_open_claim_reclaims_after_restart_with_new_generation(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        batch_id = _approved(store)
        old_claim = store.claim_price_delivery_attempt(batch_id, "p")
        assert old_claim is not None

    with DeliveryStore(database) as restarted:
        new_claim = restarted.claim_price_delivery_attempt(batch_id, "p")
        assert new_claim == PriceDeliveryClaim(batch_id, "p", 2)
        assert not restarted.release_price_delivery_attempt(old_claim)
        assert restarted.release_price_delivery_attempt(new_claim)


def test_claim_generations_are_not_a_lifetime_ten_attempt_cap(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        batch_id = _approved(store)
        for generation in range(1, 12):
            claim = store.claim_price_delivery_attempt(batch_id, "p")
            assert claim == PriceDeliveryClaim(batch_id, "p", generation)
            if generation < 11:
                assert store.release_price_delivery_attempt(claim)
        store.record_approved_price_delivery(claim, _change().current)


def test_normal_cursor_survives_restart_and_candidate_retirement(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        item = _change()
        fingerprint = canonical_manifest_fingerprint(
            feed_id="feed",
            provider="test",
            items=(item,),
        )
        store.record_price_change_candidate(
            feed_id="feed",
            provider="test",
            fingerprint=fingerprint,
            catalog_count=1,
            available_count=1,
            items=(item,),
        )
        assert store.select_normal_price_deliveries(
            feed_id="feed",
            product_ids=("a", "b", "c"),
            limit=2,
        ) == ("a", "b")
        assert store.load_price_batch(1).status == "revoked"  # type: ignore[union-attr]

    with DeliveryStore(database) as restarted:
        assert restarted.select_normal_price_deliveries(
            feed_id="feed",
            product_ids=("a", "b", "c"),
            limit=2,
        ) == ("c", "a")
        assert (
            restarted.select_normal_price_deliveries(
                feed_id="feed",
                product_ids=(),
                limit=2,
            )
            == ()
        )


def test_candidate_approval_and_normal_selection_have_one_winner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        item = _change()
        fingerprint = canonical_manifest_fingerprint(
            feed_id="feed",
            provider="test",
            items=(item,),
        )
        store.record_price_change_candidate(
            feed_id="feed",
            provider="test",
            fingerprint=fingerprint,
            catalog_count=1,
            available_count=1,
            items=(item,),
        )
        assert store.select_normal_price_deliveries(
            feed_id="feed",
            product_ids=("p",),
        ) == ("p",)
        with pytest.raises(ValueError, match="not pending"):
            store.approve_price_change_batch(
                feed_id="feed",
                fingerprint=fingerprint,
                reason="too late",
            )

    with DeliveryStore(database) as store:
        item = _change("q")
        fingerprint = canonical_manifest_fingerprint(
            feed_id="feed",
            provider="test",
            items=(item,),
        )
        store.record_price_change_candidate(
            feed_id="feed",
            provider="test",
            fingerprint=fingerprint,
            catalog_count=1,
            available_count=1,
            items=(item,),
        )
        store.approve_price_change_batch(
            feed_id="feed",
            fingerprint=fingerprint,
            reason="wins first",
        )
        assert (
            store.select_normal_price_deliveries(
                feed_id="feed",
                product_ids=("q",),
            )
            is None
        )


def test_recurring_terminal_fingerprints_do_not_capture_current_revoke(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        for product_id in ("a", "b", "a", "b"):
            batch_id = _approved(store, product_id)
            claim = store.claim_price_delivery_attempt(batch_id, product_id)
            assert claim is not None
            store.record_approved_price_delivery(claim, _change(product_id).current)
            assert store.complete_price_change_batch_if_drained(batch_id)

        item = _change("a")
        fingerprint = canonical_manifest_fingerprint(
            feed_id="feed",
            provider="test",
            items=(item,),
        )
        store.record_price_change_candidate(
            feed_id="feed",
            provider="test",
            fingerprint=fingerprint,
            catalog_count=1,
            available_count=1,
            items=(item,),
        )
        revoked = store.revoke_price_change_batch(
            feed_id="feed",
            fingerprint=fingerprint,
            reason="withdraw recurring candidate",
        )
        assert revoked.status == "revoked"


def test_baseline_duplicate_approval_preserves_audit(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        candidate = store.record_feed_baseline_candidate(
            feed_id="feed",
            entry_ids=("a", "b"),
            reason="inventory review",
        )
        approved = store.approve_feed_baseline_candidate(
            feed_id="feed",
            fingerprint=candidate.fingerprint,
            reason="approved once",
        )
        duplicate = store.approve_feed_baseline_candidate(
            feed_id="feed",
            fingerprint=candidate.fingerprint,
            reason="do not rewrite",
        )
        assert duplicate == approved
