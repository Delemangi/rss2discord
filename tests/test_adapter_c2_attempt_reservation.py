from decimal import Decimal
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.recovery_models import PriceChangeRecord
from rss2discord.transports.price_monitor import prepare_price_delivery


def test_selection_is_fair_after_reopen_without_claiming_before_delivery(
    tmp_path: Path,
) -> None:
    records = tuple(
        PriceChangeRecord(
            str(i),
            PriceSnapshot("feed", str(i), Decimal(100), "100 MKD", "MKD"),
            PriceSnapshot("feed", str(i), Decimal(90), "90 MKD", "MKD"),
        )
        for i in range(3)
    )
    fingerprint = canonical_manifest_fingerprint(
        feed_id="feed",
        provider="setec",
        items=records,
    )
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        store.upsert_price_snapshots(record.previous for record in records)
        candidate = store.record_price_change_candidate(
            feed_id="feed",
            provider="setec",
            fingerprint=fingerprint,
            catalog_count=3,
            available_count=3,
            items=records,
        )
        store.approve_price_change_batch(
            feed_id="feed",
            fingerprint=fingerprint,
            reason="fixture review",
        )
        # Model repeated failures, then an interrupted scan with uneven counts.
        for index in range(37):
            claim = store.claim_price_delivery_attempt(
                candidate.batch_id,
                str(index % 3),
            )
            assert claim is not None
            assert store.release_price_delivery_attempt(claim)
    with DeliveryStore(path) as store:
        plan = prepare_price_delivery(
            store=store,
            feed_id="feed",
            provider="setec",
            changes=records,
            current={r.product_id: r.current for r in records},
            persisted={r.product_id: r.previous for r in records},
            catalog_count=3,
        )
        assert plan.selected_ids == ("1", "2", "0")
        batch = store.load_active_price_batch("feed")
        assert batch is not None
        assert {item.attempt_count for item in batch.items} == {12, 13}
        assert batch.status == "approved"
        next_plan = prepare_price_delivery(
            store=store,
            feed_id="feed",
            provider="setec",
            changes=records,
            current={r.product_id: r.current for r in records},
            persisted={r.product_id: r.previous for r in records},
            catalog_count=3,
        )
        assert next_plan == plan
        assert store.load_active_price_batch("feed") == batch
