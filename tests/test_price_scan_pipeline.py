"""Shared provider scan pipeline evidence."""

from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData, PriceSnapshot
from rss2discord.price_notifications import PriceNotification
from rss2discord.retries import FeedFetchInterruptedError, SQLiteRetryPolicy
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceDeliveryPlan,
    PriceObservation,
    run_price_scan,
)
from rss2discord.transports.price_monitor import scan as scan_module
from tests.setec_price_monitor_helpers import RecordingSender


def feed(feed_id: str = "feed") -> FeedConfig:
    return FeedConfig(
        id=feed_id,
        url=f"https://shop.example.test/{feed_id}",
        webhook="https://discord.example.test/hook",
        strategy="anhoch",
    )


def snapshot(
    product_id: str,
    amount: int = 100,
    *,
    currency: str = "MKD",
) -> PriceSnapshot:
    return PriceSnapshot(
        "feed",
        product_id,
        Decimal(amount),
        f"{amount} {currency}",
        currency,
    )


def dependencies(
    store: DeliveryStore,
    sender: RecordingSender,
    *,
    shutdown: Callable[[], bool] = lambda: False,
) -> SimpleNamespace:
    return SimpleNamespace(
        snapshots=store,
        sender=sender,
        sqlite_retry_policy=SQLiteRetryPolicy(
            sleep=lambda _: True,
            on_retry=lambda *_: None,
        ),
        delivery=PriceAlertDelivery(
            sleep=lambda _: True,
            delay_between_posts=0,
            is_shutdown_requested=shutdown,
        ),
    )


def message_for(
    product: str,
    previous: PriceSnapshot,
    current: PriceSnapshot,
) -> WebhookMessage:
    del previous
    return WebhookMessage(
        feed=feed(current.feed_id),
        entry=EntryData(
            title=f"Product {product}",
            link=f"https://shop.example.test/{product}",
            description="",
            author="",
            timestamp=None,
        ),
        source_title="Shop",
    )


def run(
    store: DeliveryStore,
    sender: RecordingSender,
    observations: list[PriceObservation[str]],
    *,
    limit: int = 100,
    confirm: Callable[
        [Sequence[PriceObservation[str]]],
        Sequence[PriceObservation[str]],
    ]
    | None = None,
    confirmation_limit: int | None = None,
    shutdown: Callable[[], bool] = lambda: False,
    feed_id: str = "feed",
) -> None:
    run_price_scan(
        feed=feed(feed_id),
        dependencies=dependencies(store, sender, shutdown=shutdown),
        provider="test",
        label="Test",
        observations=observations,
        message_for=message_for,
        snapshot_limit=limit,
        confirm=confirm,
        confirmation_limit=confirmation_limit,
    )


def seed(store: DeliveryStore, items: tuple[PriceSnapshot, ...]) -> None:
    store.upsert_price_snapshots(items)


def test_snapshot_limit_rejects_before_any_delivery(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, tuple(snapshot(str(index)) for index in range(3)))
        sender = RecordingSender([])
        with pytest.raises(FeedFetchError, match="SnapshotLimitExceeded"):
            run(store, sender, [], limit=2)
        assert sender.messages == []


def test_no_change_scan_drains_pending_outbox_and_records_health(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        current = snapshot("1")
        seed(store, (current,))
        entry = message_for("1", current, current).entry
        store.record_price_scan(
            feed_id="feed",
            group_id="group",
            observed_at=100,
            baseline_updates=(current,),
            notifications=(PriceNotification("1", None, current, entry, "Shop"),),
        )
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
        run(store, sender, [PriceObservation("1", current)])
        assert len(sender.messages) == 1
        assert store.count_pending_price_notifications("feed") == 0
        health = store.list_health("feed")[0]
        assert (health.state, health.item_count) == ("healthy", 1)


def test_inline_and_bulk_boundary(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        inline_ids = tuple(str(index) for index in range(25))
        seed(
            store,
            tuple(snapshot(product_id) for product_id in inline_ids),
        )
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED] * 25)
        run(
            store,
            sender,
            [
                PriceObservation(product_id, snapshot(product_id, 90))
                for product_id in inline_ids
            ],
            feed_id="feed",
        )
        assert len(sender.messages) == 25

    with DeliveryStore(tmp_path / "digest.db") as store:
        digest_ids = tuple(str(index) for index in range(201))
        seed(store, tuple(snapshot(product_id) for product_id in digest_ids))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
        run(
            store,
            sender,
            [
                PriceObservation(product_id, snapshot(product_id, 90))
                for product_id in digest_ids
            ],
            feed_id="feed",
            limit=500,
        )
        assert len(sender.messages) == 1
        assert len(sender.messages[0].files) == 2


def test_confirm_all_for_inline_scan(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        ids = ("1", "2", "3")
        seed(store, tuple(snapshot(product_id) for product_id in ids))
        seen: list[tuple[str, ...]] = []

        def confirm(
            observations: Sequence[PriceObservation[str]],
        ) -> Sequence[PriceObservation[str]]:
            seen.append(tuple(item.snapshot.product_id for item in observations))
            return observations

        sender = RecordingSender([DiscordDeliveryResult.DELIVERED] * 3)
        run(
            store,
            sender,
            [
                PriceObservation(product_id, snapshot(product_id, 90))
                for product_id in ids
            ],
            confirm=confirm,
        )
        assert seen == [ids]
        assert len(sender.messages) == 3


def test_rotated_confirmation_reports_additional_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        ids = tuple(str(index) for index in range(26))
        seed(store, tuple(snapshot(product_id) for product_id in ids))
        seen: list[tuple[str, ...]] = []
        plans: list[PriceDeliveryPlan] = []
        prepare = scan_module.prepare_price_delivery

        def capture(**kwargs: object) -> PriceDeliveryPlan:
            plan = prepare(**kwargs)  # type: ignore[arg-type]
            plans.append(plan)
            return plan

        def confirm(
            observations: Sequence[PriceObservation[str]],
        ) -> Sequence[PriceObservation[str]]:
            seen.append(tuple(item.snapshot.product_id for item in observations))
            return observations[:4]

        monkeypatch.setattr(scan_module, "prepare_price_delivery", capture)
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED] * 4)
        run(
            store,
            sender,
            [
                PriceObservation(product_id, snapshot(product_id, 90))
                for product_id in ids
            ],
            confirm=confirm,
            confirmation_limit=10,
        )
        assert len(seen[0]) == 10
        assert plans[0].bulk is True
        assert plans[0].additional_pending == 22
        assert len(sender.messages) == 4


def test_rotated_confirmation_note_reaches_digest(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        ids = tuple(str(index) for index in range(60))
        seed(store, tuple(snapshot(product_id) for product_id in ids))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
        run(
            store,
            sender,
            [
                PriceObservation(product_id, snapshot(product_id, 90))
                for product_id in ids
            ],
            confirm=lambda observations: observations,
            confirmation_limit=30,
        )
        assert len(sender.messages) == 1
        assert "30 additional changes are pending confirmation" in (
            sender.messages[0].entry.description
        )


def test_no_confirmed_change_records_deferral(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        ids = tuple(str(index) for index in range(26))
        seed(store, tuple(snapshot(product_id) for product_id in ids))
        sender = RecordingSender([])
        run(
            store,
            sender,
            [
                PriceObservation(product_id, snapshot(product_id, 90))
                for product_id in ids
            ],
            confirm=lambda _observations: (),
            confirmation_limit=10,
        )
        assert sender.messages == []
        health = store.list_health("feed")[0]
        assert (health.state, health.cause) == (
            "failed",
            "PriceConfirmationDeferred",
        )
        assert all(item.amount == 100 for item in store.load_price_snapshots("feed"))


def test_silent_updates_are_persisted_on_inline_and_bulk_paths(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, (snapshot("1"),))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
        run(
            store,
            sender,
            [
                PriceObservation("1", snapshot("1", 90)),
                PriceObservation("2", snapshot("2", 200)),
            ],
        )
        persisted = {
            item.product_id: item for item in store.load_price_snapshots("feed")
        }
        assert persisted["2"].amount == 200

    with DeliveryStore(tmp_path / "bulk.db") as store:
        ids = tuple(str(index) for index in range(26))
        seed(store, tuple(snapshot(product_id) for product_id in ids))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
        run(
            store,
            sender,
            [
                *(
                    PriceObservation(product_id, snapshot(product_id, 90))
                    for product_id in ids
                ),
                PriceObservation("silent", snapshot("silent", 5)),
            ],
        )
        persisted = {
            item.product_id: item for item in store.load_price_snapshots("feed")
        }
        assert persisted["silent"].amount == 5


@pytest.mark.parametrize("checkpoint", [1, 2, 3])
def test_shutdown_checkpoints_stop_before_mutation(
    tmp_path: Path,
    checkpoint: int,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, (snapshot("1"),))
        checks = 0

        def shutdown() -> bool:
            nonlocal checks
            checks += 1
            return checks == checkpoint

        sender = RecordingSender([])
        with pytest.raises(FeedFetchInterruptedError):
            run(
                store,
                sender,
                [PriceObservation("1", snapshot("1", 90))],
                shutdown=shutdown,
            )
        assert sender.messages == []
        assert all(item.amount == 100 for item in store.load_price_snapshots("feed"))
        assert store.list_health("feed") == ()


def test_currency_mismatch_raises_before_delivery(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, (snapshot("1"),))
        sender = RecordingSender([])
        with pytest.raises(FeedFetchError, match="CurrencyChanged"):
            run(
                store,
                sender,
                [PriceObservation("1", snapshot("1", 90, currency="EUR"))],
            )
        assert sender.messages == []
        assert all(item.amount == 100 for item in store.load_price_snapshots("feed"))


def _seed_pending_notification(store: DeliveryStore) -> None:
    current = snapshot("2")
    store.record_price_scan(
        feed_id="feed",
        group_id="pending",
        observed_at=100,
        baseline_updates=(current,),
        notifications=(
            PriceNotification(
                product_id="2",
                previous=None,
                current=current,
                entry=message_for("2", current, current).entry,
                source_title="Shop",
            ),
        ),
    )


def test_failed_pending_drain_keeps_failed_health(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, (snapshot("1"),))
        _seed_pending_notification(store)
        sender = RecordingSender(
            [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.DELIVERED],
        )
        run(store, sender, [PriceObservation("1", snapshot("1"))])
        health = store.list_health("feed")[0]
        assert (health.state, health.cause) == ("failed", "DeliveryRetryPending")
        assert store.count_pending_price_notifications("feed") == 1
        run(store, sender, [PriceObservation("1", snapshot("1"))])
        assert store.count_pending_price_notifications("feed") == 0
        assert store.list_health("feed")[0].state == "healthy"


def test_failed_card_records_failed_health_and_retries(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, (snapshot("1"),))
        sender = RecordingSender(
            [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.DELIVERED],
        )
        run(store, sender, [PriceObservation("1", snapshot("1", 90))])
        health = store.list_health("feed")[0]
        assert (health.state, health.cause) == ("failed", "DeliveryRetryPending")
        assert all(item.amount == 100 for item in store.load_price_snapshots("feed"))
        run(store, sender, [PriceObservation("1", snapshot("1", 90))])
        assert store.list_health("feed")[0].state == "healthy"
        assert store.load_price_snapshots("feed")[0].amount == 90


def test_bulk_health_reports_available_catalog_count(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        ids = tuple(str(index) for index in range(201))
        seed(store, tuple(snapshot(product_id) for product_id in ids))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
        run(
            store,
            sender,
            [
                PriceObservation(product_id, snapshot(product_id, 90))
                for product_id in ids
            ],
            limit=500,
        )
        assert store.list_health("feed")[0].item_count == 201
