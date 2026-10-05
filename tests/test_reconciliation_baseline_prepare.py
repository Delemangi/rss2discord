import json
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.admin import main
from rss2discord.configuration import FeedConfig
from rss2discord.database_ownership import DatabaseOwnership
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.reconciliation import apply_plan, create_plan
from rss2discord.recovery_models import PriceChangeRecord, PriceSnapshot
from rss2discord.transports.catalog_normalization import (
    CatalogObservation,
    normalize_ddstore_catalog,
)
from tests.reconciliation_helpers import setup_plan
from tests.test_ddstore_price_monitor import make_feed, make_product

SOURCE_URL = "https://ddstore.mk/shop/"


def _feed() -> FeedConfig:
    return make_feed().model_copy(update={"url": SOURCE_URL})


def _config(path: Path, *, url: str = SOURCE_URL, feed_id: str = "ddstore") -> Path:
    feed = _feed().model_copy(update={"url": url, "id": feed_id})
    path.write_text(
        json.dumps({"feeds": [feed.model_dump(mode="json")]}),
        encoding="utf-8",
    )
    return path


def _prepare_args(
    database: Path,
    config: Path,
    fingerprint: str,
    *,
    feed_id: str = "ddstore",
) -> list[str]:
    return [
        "--database",
        str(database),
        "baseline",
        "prepare",
        "--config",
        str(config),
        "--feed-id",
        feed_id,
        "--reconciliation-fingerprint",
        fingerprint,
        "--writers-stopped",
        "--reason",
        "verified inventory recovery",
    ]


def _apply_with_missing_and_held_history(database: Path) -> str:
    feed = _feed()
    with DeliveryStore(database) as store:
        first_draft = setup_plan(store)
        first_batch = store.load_price_batch(first_draft.batch_id)
        assert first_batch is not None
        first = create_plan(
            store,
            feed,
            batch_id=first_batch.batch_id,
            batch_fingerprint=first_batch.fingerprint,
            reason="create historical availability hold",
            version=2,
            catalog_fetch=lambda current_feed, snapshots: normalize_ddstore_catalog(
                current_feed.id,
                (make_product("1", amount=90),),
                snapshots,
            ),
        )
        first = first.model_copy(
            update={
                "items": tuple(
                    item.model_copy(
                        update={
                            "disposition": "defer"
                            if item.product_id == "2"
                            else "accept",
                        },
                    )
                    for item in first.items
                ),
            },
        ).sealed()
        with DatabaseOwnership(database) as ownership:
            apply_plan(
                store,
                feed,
                first,
                ownership,
                fingerprint=first.fingerprint(),
                catalog_fetch=lambda current_feed, snapshots: normalize_ddstore_catalog(
                    current_feed.id,
                    (make_product("1", amount=90),),
                    snapshots,
                ),
            )

        # Applying a reconciliation must not implicitly prepare a baseline.
        assert not store.has_complete_baseline(feed.id)
        assert store.load_baseline_candidate(feed.id) is None

        store.mark_delivered(feed.id, "historical-delivery")
        store.upsert_price_snapshots(
            (
                PriceSnapshot(
                    feed.id,
                    "unpriced-missing",
                    Decimal(12),
                    "12 MKD",
                    "MKD",
                ),
            ),
        )
        previous = next(
            snapshot
            for snapshot in store.load_price_snapshots(feed.id)
            if snapshot.product_id == "1"
        )
        current = PriceSnapshot(feed.id, "1", Decimal(95), "95 MKD", "MKD")
        record = PriceChangeRecord("1", previous, current)
        second_batch = store.record_price_change_candidate(
            feed_id=feed.id,
            provider="DDStore",
            fingerprint=canonical_manifest_fingerprint(
                feed_id=feed.id,
                provider="DDStore",
                items=(record,),
            ),
            catalog_count=2,
            available_count=2,
            items=(record,),
        )

        def second_catalog(
            current_feed: FeedConfig,
            snapshots: tuple[PriceSnapshot, ...],
        ) -> tuple[CatalogObservation, ...]:
            return normalize_ddstore_catalog(
                current_feed.id,
                (
                    make_product("1", amount=95),
                    make_product("new-eligible", amount=50),
                ),
                snapshots,
            )

        draft = create_plan(
            store,
            feed,
            batch_id=second_batch.batch_id,
            batch_fingerprint=second_batch.fingerprint,
            reason="review complete future inventory",
            version=2,
            catalog_fetch=second_catalog,
        )
        choices = {
            "1": "accept",
            "2": "hold",
            "new-eligible": "accept",
            "unpriced-missing": "defer",
        }
        reviewed = draft.model_copy(
            update={
                "items": tuple(
                    item.model_copy(update={"disposition": choices[item.product_id]})
                    for item in draft.items
                ),
            },
        ).sealed()
        assert set(choices) == {item.product_id for item in reviewed.items}
        with DatabaseOwnership(database) as ownership:
            receipt = apply_plan(
                store,
                feed,
                reviewed,
                ownership,
                fingerprint=reviewed.fingerprint(),
                catalog_fetch=second_catalog,
            )
        assert receipt["plan_json"] == reviewed.model_dump_json()
        hold = next(
            hold
            for hold in store.list_price_product_holds(feed.id)
            if hold["product_id"] == "2"
        )
        assert hold["kind"] == "availability"
        return reviewed.fingerprint()


def test_baseline_prepare_requires_an_applied_receipt_and_matching_fingerprint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database):
        pass
    config = _config(tmp_path / "config.yaml")
    assert main(_prepare_args(database, config, "0" * 64)) == 2
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--database",
                str(database),
                "baseline",
                "prepare",
                "--config",
                str(config),
                "--feed-id",
                "ddstore",
                "--reconciliation-fingerprint",
                "0" * 64,
                "--reason",
                "missing required lock acknowledgment",
            ],
        )
    assert error.value.code == 2


def test_baseline_prepare_uses_full_receipted_identity_set_and_approval_is_separate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "state.db"
    fingerprint = _apply_with_missing_and_held_history(database)
    config = _config(tmp_path / "config.yaml")

    assert main(_prepare_args(database, config, fingerprint)) == 0
    output = json.loads(capsys.readouterr().out)
    candidate_fingerprint = output["fingerprint"]
    with DeliveryStore(database, read_only=True) as store:
        candidate = store.load_baseline_candidate("ddstore")
        assert candidate is not None
        assert candidate.entry_ids == (
            "1",
            "2",
            "new-eligible",
            "unpriced-missing",
        )
        assert candidate.status == "candidate"
        assert not store.has_complete_baseline("ddstore")
        assert store.count_delivered("ddstore") == 1
        assert store.has_delivered("ddstore", "historical-delivery")
        assert not store.has_delivered("ddstore", "new-eligible")

    assert main(_prepare_args(database, config, fingerprint)) == 0
    assert json.loads(capsys.readouterr().out)["fingerprint"] == candidate_fingerprint
    with DeliveryStore(database, read_only=True) as store:
        assert not store.has_complete_baseline("ddstore")

    assert (
        main(
            [
                "--database",
                str(database),
                "baseline",
                "approve",
                "--feed-id",
                "ddstore",
                "--fingerprint",
                candidate_fingerprint,
                "--reason",
                "separately reviewed inventory",
            ],
        )
        == 0
    )
    capsys.readouterr()
    with DeliveryStore(database, read_only=True) as store:
        assert store.has_complete_baseline("ddstore")
        assert store.has_baselined("ddstore", "historical-delivery") is False
        assert store.has_baselined("ddstore", "new-eligible")
        assert store.has_handled_entry("ddstore", "new-eligible")
        assert not store.has_delivered("ddstore", "new-eligible")
        assert store.count_delivered("ddstore") == 1
        assert store.has_delivered("ddstore", "historical-delivery")


def test_baseline_prepare_refuses_wrong_feed_source_and_writer_ownership(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    fingerprint = _apply_with_missing_and_held_history(database)
    assert (
        main(
            _prepare_args(
                database,
                _config(
                    tmp_path / "wrong-source.yaml",
                    url="https://ddstore.mk/other/",
                ),
                fingerprint,
            ),
        )
        == 2
    )
    assert (
        main(
            _prepare_args(
                database,
                _config(tmp_path / "wrong-feed.yaml", feed_id="other"),
                fingerprint,
                feed_id="other",
            ),
        )
        == 2
    )
    with DatabaseOwnership(database):
        assert (
            main(
                _prepare_args(
                    database,
                    _config(tmp_path / "config.yaml"),
                    fingerprint,
                ),
            )
            == 2
        )
    with DeliveryStore(database, read_only=True) as store:
        assert store.load_baseline_candidate("ddstore") is None


def test_baseline_prepare_refuses_incompatible_complete_baseline(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    fingerprint = _apply_with_missing_and_held_history(database)
    with DeliveryStore(database) as store:
        candidate = store.record_feed_baseline_candidate(
            feed_id="ddstore",
            entry_ids=("incompatible-existing",),
            reason="existing inventory approval",
        )
    assert (
        main(
            [
                "--database",
                str(database),
                "baseline",
                "approve",
                "--feed-id",
                "ddstore",
                "--fingerprint",
                candidate.fingerprint,
                "--reason",
                "existing inventory approval",
            ],
        )
        == 0
    )
    assert (
        main(
            _prepare_args(database, _config(tmp_path / "config.yaml"), fingerprint),
        )
        == 2
    )
