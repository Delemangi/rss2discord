"""Restock detection, scan classification, and digest counting."""

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import (
    EntryData,
    PriceChangeRecord,
    PriceSnapshot,
)
from rss2discord.price_notifications import PriceNotificationRecord
from rss2discord.retries import SQLiteRetryPolicy
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceObservation,
    diff_price_snapshots,
    diff_stock_snapshots,
    price_direction,
    run_price_scan,
)
from rss2discord.transports.price_monitor.digest import build_digest_messages
from tests.setec_price_monitor_helpers import RecordingSender


def feed(
    *,
    restock_alerts: bool = False,
    price_ignore_increases: bool = False,
    price_min_change_percentage: float = 0,
    price_min_change_amount: Decimal | None = None,
    price_inline_limit: int = 100,
) -> FeedConfig:
    return FeedConfig(
        id="feed",
        url="https://shop.example.test/feed",
        webhook="https://discord.example.test/hook",
        strategy="anhoch",
        restock_alerts=restock_alerts,
        price_ignore_increases=price_ignore_increases,
        price_min_change_percentage=price_min_change_percentage,
        price_min_change_amount=price_min_change_amount,
        price_inline_limit=price_inline_limit,
    )


def snapshot(
    product_id: str = "1",
    amount: int = 100,
    *,
    available: bool | None = None,
) -> PriceSnapshot:
    return PriceSnapshot(
        "feed",
        product_id,
        Decimal(amount),
        f"{amount} MKD",
        "MKD",
        available=available,
    )


def dependencies(store: DeliveryStore, sender: RecordingSender) -> SimpleNamespace:
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
            is_shutdown_requested=lambda: False,
        ),
    )


def message_for(
    product: str,
    previous: PriceSnapshot,
    current: PriceSnapshot,
) -> WebhookMessage:
    return WebhookMessage(
        feed=feed(),
        entry=EntryData(
            title=f"Product {product}",
            link=f"https://shop.example.test/{product}",
            description="",
            author="",
            timestamp=None,
            price_direction=price_direction(previous, current),
        ),
        source_title="Shop",
    )


def run(
    store: DeliveryStore,
    sender: RecordingSender,
    observations: Sequence[PriceObservation[str]],
    *,
    restock_alerts: bool = True,
    price_ignore_increases: bool = False,
    price_min_change_percentage: float = 0,
    price_min_change_amount: Decimal | None = None,
    price_inline_limit: int = 100,
) -> None:
    run_price_scan(
        feed=feed(
            restock_alerts=restock_alerts,
            price_ignore_increases=price_ignore_increases,
            price_min_change_percentage=price_min_change_percentage,
            price_min_change_amount=price_min_change_amount,
            price_inline_limit=price_inline_limit,
        ),
        dependencies=dependencies(store, sender),
        provider="test",
        label="Test",
        observations=observations,
        message_for=message_for,
        snapshot_limit=1000,
    )


# Pure comparison -------------------------------------------------------


def test_diff_stock_reports_a_restock_record_when_stock_returns() -> None:
    previous = snapshot(amount=100, available=False)
    current = snapshot(amount=100, available=True)

    silent, restocks = diff_stock_snapshots((current,), {"1": previous})

    assert silent == ()
    assert restocks == (PriceChangeRecord("1", previous, current),)


def test_diff_stock_is_silent_for_a_new_product() -> None:
    current = snapshot(available=True)

    silent, restocks = diff_stock_snapshots((current,), {})

    assert (silent, restocks) == ((), ())


def test_diff_stock_persists_unknown_previous_availability_silently() -> None:
    previous = snapshot(available=None)
    current = snapshot(available=True)

    silent, restocks = diff_stock_snapshots((current,), {"1": previous})

    assert silent == (current,)
    assert restocks == ()


def test_diff_stock_is_silent_when_a_product_goes_out_of_stock() -> None:
    previous = snapshot(available=True)
    current = snapshot(available=False)

    silent, restocks = diff_stock_snapshots((current,), {"1": previous})

    assert silent == (current,)
    assert restocks == ()


def test_diff_stock_does_nothing_when_availability_is_unchanged() -> None:
    current = snapshot(available=True)

    assert diff_stock_snapshots((current,), {"1": snapshot(available=True)}) == ((), ())


def test_diff_stock_ignores_an_observation_that_reports_no_availability() -> None:
    previous = snapshot(available=False)
    current = snapshot(available=None)

    assert diff_stock_snapshots((current,), {"1": previous}) == ((), ())


def test_diff_price_persists_an_availability_only_change_silently() -> None:
    previous = snapshot(available=True)
    current = snapshot(available=False)

    silent, changes = diff_price_snapshots((current,), {"1": previous})

    assert silent == (current,)
    assert changes == ()


# Scan integration ------------------------------------------------------


def test_restock_with_equal_price_alerts_once_and_persists_availability(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot(amount=100, available=False))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        run(
            store,
            sender,
            [PriceObservation("1", snapshot(amount=100, available=True))],
        )

        assert len(sender.messages) == 1
        entry = sender.messages[0].entry
        assert entry.restocked is True
        assert entry.price_direction is None
        assert entry.price_change_percentage is None
        persisted = store.load_price_snapshots("feed")[0]
        assert persisted.amount == Decimal(100)
        assert persisted.available is True

        run(
            store,
            sender,
            [PriceObservation("1", snapshot(amount=100, available=True))],
        )

        assert len(sender.messages) == 1


def test_restock_with_a_price_change_alerts_once_with_a_percentage(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot(amount=100, available=False))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        run(
            store,
            sender,
            [PriceObservation("1", snapshot(amount=90, available=True))],
        )

        assert len(sender.messages) == 1
        entry = sender.messages[0].entry
        assert entry.restocked is True
        assert entry.price_direction is None
        assert entry.price_change_percentage == "-10.0%"
        persisted = store.load_price_snapshots("feed")[0]
        assert (persisted.amount, persisted.available) == (Decimal(90), True)


def test_restock_burst_uses_one_digest_below_a_high_inline_limit(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        ids = tuple(str(index) for index in range(30))
        store.upsert_price_snapshots(
            tuple(snapshot(product_id, available=False) for product_id in ids),
        )
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        run(
            store,
            sender,
            [
                PriceObservation(product_id, snapshot(product_id, available=True))
                for product_id in ids
            ],
            price_inline_limit=200,
        )

        assert len(sender.messages) == 1
        assert sender.messages[0].entry.title == "Price changes"
        assert len(sender.messages[0].files) == 2


def test_out_of_stock_price_change_is_silent_and_persists_the_new_baseline(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot(amount=100, available=True))
        sender = RecordingSender([])

        run(
            store,
            sender,
            [PriceObservation("1", snapshot(amount=90, available=False))],
        )

        assert sender.messages == []
        persisted = store.load_price_snapshots("feed")[0]
        assert persisted.amount == Decimal(90)
        assert persisted.available is False


def test_restock_is_silent_when_restock_alerts_are_disabled(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot(amount=100, available=False))
        sender = RecordingSender([])

        run(
            store,
            sender,
            [PriceObservation("1", snapshot(amount=100, available=True))],
            restock_alerts=False,
        )

        assert sender.messages == []
        assert store.load_price_snapshots("feed")[0].available is True


def test_unknown_availability_never_alerts(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot(amount=100, available=None))
        sender = RecordingSender([])

        run(
            store,
            sender,
            [PriceObservation("1", snapshot(amount=100, available=True))],
        )

        assert sender.messages == []
        assert store.load_price_snapshots("feed")[0].available is True

        run(
            store,
            sender,
            [PriceObservation("1", snapshot(amount=100, available=True))],
        )

        assert sender.messages == []


def test_price_filters_do_not_suppress_a_restock(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshots(
            (
                snapshot("restock", 100, available=False),
                snapshot("plain", 100, available=True),
            ),
        )
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        run(
            store,
            sender,
            [
                PriceObservation("restock", snapshot("restock", 101, available=True)),
                PriceObservation("plain", snapshot("plain", 101, available=True)),
            ],
            price_ignore_increases=True,
            price_min_change_percentage=50,
            price_min_change_amount=Decimal(100),
        )

        assert len(sender.messages) == 1
        assert sender.messages[0].entry.restocked is True
        assert sender.messages[0].entry.title == "Product restock"
        persisted = {
            item.product_id: item for item in store.load_price_snapshots("feed")
        }
        # The filtered ordinary increase is silent but still advances its baseline.
        assert persisted["plain"].amount == Decimal(101)
        assert persisted["restock"].amount == Decimal(101)


def test_restock_takes_precedence_over_a_price_change_record(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot(amount=100, available=False))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        run(
            store,
            sender,
            [PriceObservation("1", snapshot(amount=80, available=True))],
        )

        assert len(sender.messages) == 1
        assert sender.messages[0].entry.restocked is True
        assert sender.messages[0].entry.price_change_percentage == "-20.0%"


def test_failed_delivery_does_not_advance_a_change_baseline_silently(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        # Unknown availability is a silent first observation, but the same scan
        # also detected a price change that still has to reach the outbox path.
        store.upsert_price_snapshot(snapshot(amount=100, available=None))
        sender = RecordingSender(
            [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.DELIVERED],
        )

        run(
            store,
            sender,
            [PriceObservation("1", snapshot(amount=90, available=True))],
        )

        persisted = store.load_price_snapshots("feed")[0]
        assert persisted.amount == Decimal(100)
        assert persisted.available is None

        run(
            store,
            sender,
            [PriceObservation("1", snapshot(amount=90, available=True))],
        )

        assert len(sender.messages) == 2
        persisted = store.load_price_snapshots("feed")[0]
        assert (persisted.amount, persisted.available) == (Decimal(90), True)


def test_restock_and_price_change_keep_observation_order(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshots(
            (
                snapshot("a", 100, available=True),
                snapshot("b", 100, available=False),
            ),
        )
        sender = RecordingSender(
            [DiscordDeliveryResult.DELIVERED, DiscordDeliveryResult.DELIVERED],
        )

        run(
            store,
            sender,
            [
                PriceObservation("b", snapshot("b", 100, available=True)),
                PriceObservation("a", snapshot("a", 90, available=True)),
            ],
        )

        assert [message.entry.title for message in sender.messages] == [
            "Product b",
            "Product a",
        ]


# Digest ----------------------------------------------------------------


def _record(
    product_id: str,
    *,
    previous: int,
    current: int,
    restocked: bool = False,
) -> PriceNotificationRecord:
    return PriceNotificationRecord(
        notification_id=hash(product_id) & 0x7FFF,
        feed_id="feed",
        group_id="group",
        product_id=product_id,
        title=f"Product {product_id}",
        link=f"https://shop.example.test/{product_id}",
        previous_price=f"{previous} MKD",
        current_price=f"{current} MKD",
        previous_amount=Decimal(previous),
        current_amount=Decimal(current),
        currency="MKD",
        entry=EntryData(
            title=f"Product {product_id}",
            link=f"https://shop.example.test/{product_id}",
            description="",
            author="",
            timestamp=None,
            restocked=restocked,
        ),
        source_title="Shop",
        observed_at=1_700_000_000,
        attempts=0,
    )


def test_digest_counts_restocks_separately_from_price_moves() -> None:
    messages = build_digest_messages(
        feed(restock_alerts=True),
        (
            _record("restock", previous=100, current=100, restocked=True),
            _record("drop", previous=100, current=90),
        ),
    )

    description = messages[0].entry.description

    assert "1 restocked" in description
    assert "1 dropped" in description
    assert "unchanged" not in description
    assert "100 MKD → 100 MKD" not in description


def test_digest_keeps_a_restock_with_a_real_move_in_the_drop_list() -> None:
    messages = build_digest_messages(
        feed(restock_alerts=True),
        (_record("restock", previous=100, current=80, restocked=True),),
    )

    description = messages[0].entry.description

    assert "1 restocked" in description
    assert "Largest drops" in description
    assert "100 MKD → 80 MKD" in description
