"""Regression evidence for summary ownership, operator races and migration."""

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.admin import main as admin_main
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult, SleepCallback
from rss2discord.discord.components import build_components_v2_payload
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.providers.gjirafa50 import background_monitor as gjirafa_background
from rss2discord.recovery_models import (
    PriceChangeRecord,
    PriceSnapshot,
    PriceSummaryClaim,
)
from rss2discord.transports.price_monitor import delivery as summary_delivery
from rss2discord.transports.price_monitor.contracts import PriceDeliveryPlan
from rss2discord.transports.price_monitor.planning import prepare_price_delivery
from tests import setec_price_monitor_helpers as setec
from tests import test_gjirafa50_price_monitor as gjirafa
from tests.test_automatic_price_summaries import records, stage
from tests.test_setec_missing_variant_identity import product_with_variant_id


def test_release_sql_error_never_leaks_os_ownership(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store, DeliveryStore(path) as other:
        batch = stage(store)
        claim = store.claim_price_summary_attempt(batch.batch_id)
        assert claim is not None
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_release BEFORE UPDATE OF claim_open ON price_change_batch_items WHEN NEW.claim_open = 0 BEGIN SELECT RAISE(ABORT, 'release failure'); END",
            )
        with pytest.raises(sqlite3.IntegrityError, match="release failure"):
            store.release_price_summary_attempt(claim)
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TRIGGER fail_release")
        fresh = other.claim_price_summary_attempt(batch.batch_id)
        assert fresh is not None
        assert fresh.generation > claim.generation
        other.record_price_summary_delivery(fresh)


def test_revoke_inside_successful_sender_acknowledges_owned_generation_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store, DeliveryStore(path) as admin:

        class Sender(setec.RecordingSender):
            def send(
                self,
                message: WebhookMessage,
                sleep: SleepCallback,
            ) -> DiscordDeliveryResult:
                batch = admin.load_active_price_batch("setec")
                assert batch is not None
                admin.revoke_price_change_batch(
                    batch.batch_id,
                    batch.fingerprint,
                    "operator revoke",
                )
                assert (
                    admin.select_normal_price_deliveries(
                        feed_id="setec",
                        product_ids=("p-000",),
                    )
                    is None
                )
                return super().send(message, sleep)

        baseline = tuple(
            setec.make_product(f"p-{i:03}", calculated_amount=100) for i in range(101)
        )
        target = tuple(setec.make_product(p.id, calculated_amount=90) for p in baseline)
        sender = Sender([DiscordDeliveryResult.DELIVERED])
        monitor = setec.make_monitor(
            setec.make_feed(),
            setec.CatalogStub([baseline, target]),
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        batch = store.list_price_change_batches("setec", include_terminal=True)[0]
        assert batch.status == "revoked"
        assert batch.reason == "operator revoke"
        assert batch.delivered_count == 101
        assert store.claim_price_summary_attempt(batch.batch_id) is None
        assert all(s.amount == 90 for s in store.load_price_snapshots("setec"))


def test_currency_validation_precedes_historical_summary_retry(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        batch = stage(store)
        current = {i.product_id: replace(i.current, currency="EUR") for i in records()}
        plan = prepare_price_delivery(
            store=store,
            feed_id="feed",
            provider="test",
            changes=(),
            current=current,
            persisted={i.product_id: i.previous for i in records()},
            catalog_count=101,
        )
        assert plan.blocked
        assert plan.summary is None
        loaded = store.load_price_batch(batch.batch_id)
        assert loaded is not None
        assert loaded.status == "paused"
        assert all(s.amount == 100 for s in store.load_price_snapshots("feed"))


def test_setec_unconfirmed_rotation_is_bounded_and_durable(tmp_path: Path) -> None:
    baseline = tuple(
        setec.make_product(f"p-{i:03}", calculated_amount=100) for i in range(401)
    )
    target = tuple(setec.make_product(p.id, calculated_amount=90) for p in baseline)
    catalog = setec.CatalogStub(
        [baseline, target, target],
        display_overrides={
            p.id: setec.make_product(p.id, calculated_amount=80) for p in baseline
        },
    )
    sender = setec.RecordingSender([])
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()
        assert not sender.messages
        assert not store.list_price_change_batches("setec")
    with DeliveryStore(path) as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        first, second = catalog.requested_id_batches
        assert len(first) == len(second) == 200
        assert not set(first).intersection(second)
        assert all(s.amount == 100 for s in store.load_price_snapshots("setec"))


def test_terminal_summary_history_is_bounded(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        for amount in range(15):
            previous = PriceSnapshot(
                "feed",
                "one",
                Decimal(100 + amount),
                str(100 + amount),
                "MKD",
            )
            current = replace(
                previous,
                amount=previous.amount + 1,
                formatted=str(101 + amount),
            )
            store.upsert_price_snapshot(previous)
            batch = store.stage_automatic_price_summary(
                feed_id="feed",
                provider="test",
                items=(PriceChangeRecord("one", previous, current),),
                catalog_count=1,
                available_count=1,
            )
            claim = store.claim_price_summary_attempt(batch.batch_id)
            assert claim is not None
            store.record_price_summary_delivery(claim)
        assert len(store.list_price_change_batches("feed", include_terminal=True)) == 10


def test_migration_of_populated_old_schema_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
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
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE price_change_batches DROP COLUMN delivery_mode")
    for _ in range(2):
        with DeliveryStore(path) as store:
            batch = store.load_price_batch(legacy.batch_id)
            assert batch is not None
            assert batch.delivery_mode == "individual"
            assert batch.items == legacy.items


def test_database_alias_uses_same_os_lock(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    alias = tmp_path / "alias.db"
    with DeliveryStore(path) as store:
        batch = stage(store)
        try:
            alias.symlink_to(path)
        except OSError as error:
            pytest.skip(f"OS symlink permissions unavailable: {error}")
        claim = store.claim_price_summary_attempt(batch.batch_id)
        assert claim is not None
        with DeliveryStore(alias) as other:
            assert other.claim_price_summary_attempt(batch.batch_id) is None
        result = subprocess.run(  # noqa: S603 - fixed interpreter and fixture-only arguments
            [
                sys.executable,
                "-c",
                (
                    "import sys; from pathlib import Path; from rss2discord.delivery_store import DeliveryStore; "
                    "s = DeliveryStore(Path(sys.argv[1])); "
                    "assert s.claim_price_summary_attempt(int(sys.argv[2])) is None; s.close()"
                ),
                str(alias),
                str(batch.batch_id),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        store.release_price_summary_attempt(claim)


@pytest.mark.parametrize("failure", ["render", "ack"])
def test_post_claim_errors_release_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store, DeliveryStore(path) as other:
        baseline = tuple(
            setec.make_product(f"p-{i:03}", calculated_amount=100) for i in range(101)
        )
        target = tuple(setec.make_product(p.id, calculated_amount=90) for p in baseline)
        sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
        monitor = setec.make_monitor(
            setec.make_feed(),
            setec.CatalogStub([baseline, target]),
            store,
            sender,
        )
        monitor.scan()
        if failure == "render":

            def fail_render(**kwargs: object) -> None:
                del kwargs
                raise RuntimeError("fixture render failure")

            monkeypatch.setattr(summary_delivery, "EntryData", fail_render)
        else:

            def fail_ack(claim: PriceSummaryClaim) -> None:
                del claim
                raise RuntimeError("fixture ack failure")

            monkeypatch.setattr(store, "record_price_summary_delivery", fail_ack)
        with pytest.raises(RuntimeError, match="fixture"):
            monitor.scan()
        batch = other.load_active_price_batch("setec")
        assert batch is not None
        claim = other.claim_price_summary_attempt(batch.batch_id)
        assert claim is not None
        other.release_price_summary_attempt(claim)
        assert all(s.amount == 100 for s in store.load_price_snapshots("setec"))


def test_retrying_local_ack_does_not_resend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        baseline = tuple(
            setec.make_product(f"p-{i:03}", calculated_amount=100) for i in range(101)
        )
        target = tuple(setec.make_product(p.id, calculated_amount=90) for p in baseline)
        sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
        monitor = setec.make_monitor(
            setec.make_feed(),
            setec.CatalogStub([baseline, target]),
            store,
            sender,
        )
        monitor.scan()
        acknowledge = store.record_price_summary_delivery
        calls = 0

        def flaky_ack(claim: PriceSummaryClaim) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            acknowledge(claim)

        monkeypatch.setattr(store, "record_price_summary_delivery", flaky_ack)
        monitor.scan()
        assert calls == 2
        assert len(sender.messages) == 1
        assert all(s.amount == 90 for s in store.load_price_snapshots("setec"))


def test_bulk_variant_mismatches_never_enter_confirmed_subset(tmp_path: Path) -> None:
    baseline = tuple(
        setec.make_product(f"p-{i:03}", calculated_amount=100) for i in range(201)
    )
    target = tuple(setec.make_product(p.id, calculated_amount=90) for p in baseline)
    overrides = {
        p.id: product_with_variant_id(p, "wrong-variant") for p in target[:150]
    }
    catalog = setec.CatalogStub([baseline, target], display_overrides=overrides)
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()
        batch = store.list_price_change_batches("setec", include_terminal=True)[0]
        assert batch.item_count == 50
        assert batch.delivered_count == 50
        assert sum(s.amount == 90 for s in store.load_price_snapshots("setec")) == 50
        assert len(catalog.requested_id_batches[0]) == 200
        assert "151 additional changes" in sender.messages[0].entry.description
        message = sender.messages[0]
        payload = build_components_v2_payload(
            message.feed,
            message.entry,
            message.source_title,
        )
        assert len(json.dumps(payload)) < 4000


def test_read_only_admin_displays_automatic_not_operator_approval(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        batch = stage(store)
    with DeliveryStore(path, read_only=True) as store:
        loaded = store.load_price_batch(batch.batch_id)
        assert loaded is not None
        assert loaded.delivery_mode == "automatic_summary"
        assert loaded.summary.approved_at is None
    assert admin_main(["--database", str(path), "price", "list"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["delivery_mode"] == "automatic_summary"
    assert rows[0]["approved_at"] is None
    assert rows[0]["reason"] == "AutomaticConfirmedBulkSummary"


def test_transient_fetch_failure_does_not_pause_automatic_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tuple(
        setec.make_product(f"p-{i:03}", calculated_amount=100) for i in range(101)
    )
    target = tuple(setec.make_product(p.id, calculated_amount=90) for p in baseline)
    catalog = setec.CatalogStub([baseline, target])
    sender = setec.RecordingSender([DiscordDeliveryResult.FAILED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()

        def fail_fetch(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise FeedFetchError("Setec", "Timeout")

        monkeypatch.setattr(catalog, "fetch_price_index", fail_fetch)
        with pytest.raises(FeedFetchError, match="Timeout"):
            monitor.scan()
        batch = store.load_active_price_batch("setec")
        assert batch is not None
        assert batch.status == "approved"
        assert batch.delivery_mode == "automatic_summary"


def test_gjirafa_worker_own_connection_delivers_summary_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.db"
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    baseline = tuple(gjirafa.make_product(i, 100) for i in range(101))
    target = tuple(gjirafa.make_product(i, 90) for i in range(101))
    with DeliveryStore(path) as store:
        direct = gjirafa.make_monitor(
            gjirafa.CatalogStub([baseline, target]),
            store,
            sender,
        )
        monkeypatch.setattr(
            gjirafa_background,
            "DiscordWebhookClient",
            lambda *, session: sender,
        )
        background = gjirafa_background.Gjirafa50BackgroundPriceMonitor(
            gjirafa.make_feed(),
            direct._dependencies,
        )
        # Exercise the real finite worker body: it opens/closes its own store.
        background._run()
        background._run()
        assert len(sender.messages) == 1
        batch = store.list_price_change_batches("gjirafa50", include_terminal=True)[0]
        assert batch.status == "completed"
        assert batch.delivered_count == 101


def test_automatic_drift_retires_then_recomputes_but_operator_pause_stays(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        original = stage(store)
        store.upsert_price_snapshot(
            replace(records()[0].previous, formatted="new display"),
        )
        persisted = {s.product_id: s for s in store.load_price_snapshots("feed")}
        fresh = tuple(replace(i, previous=persisted[i.product_id]) for i in records())

        def plan() -> PriceDeliveryPlan:
            return prepare_price_delivery(
                store=store,
                feed_id="feed",
                provider="test",
                changes=fresh,
                current={i.product_id: i.current for i in fresh},
                persisted=persisted,
                catalog_count=101,
            )

        assert plan().blocked
        retired = store.load_price_batch(original.batch_id)
        assert retired is not None
        assert retired.status == "revoked"
        replacement = plan()
        assert replacement.summary is not None
        assert replacement.summary.batch_id != original.batch_id
        store.pause_price_change_batch(replacement.summary.batch_id, "operator pause")
        assert plan().blocked
