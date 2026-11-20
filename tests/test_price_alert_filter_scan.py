"""Scan-pipeline evidence for per-feed price alert filters."""

from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.transports.price_monitor import PriceObservation, run_price_scan
from tests.setec_price_monitor_helpers import RecordingSender
from tests.test_price_scan_pipeline import (
    dependencies,
    message_for,
    seed,
    snapshot,
)


def shop(**overrides: object) -> FeedConfig:
    values: dict[str, object] = {
        "id": "feed",
        "url": "https://shop.example.test/catalog",
        "webhook": "https://discord.example.test/hook",
        "strategy": "anhoch",
    }
    values.update(overrides)
    return FeedConfig.model_validate(values)


def run(
    store: DeliveryStore,
    sender: RecordingSender,
    observations: list[PriceObservation[str]],
    *,
    feed: FeedConfig,
    confirm: (
        Callable[
            [Sequence[PriceObservation[str]]],
            Sequence[PriceObservation[str]],
        ]
        | None
    ) = None,
    confirmation_limit: int | None = None,
    limit: int = 100,
) -> None:
    run_price_scan(
        feed=feed,
        dependencies=dependencies(store, sender),
        provider="test",
        label="Test",
        observations=observations,
        message_for=message_for,
        snapshot_limit=limit,
        confirm=confirm,
        confirmation_limit=confirmation_limit,
    )


def persisted_amounts(store: DeliveryStore) -> dict[str, Decimal]:
    return {item.product_id: item.amount for item in store.load_price_snapshots("feed")}


def test_suppressed_only_scan_is_silent_but_advances_the_baseline(
    tmp_path: Path,
) -> None:
    # Given a change smaller than the configured absolute minimum
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, (snapshot("1", 1000),))
        sender = RecordingSender([])

        # When
        run(
            store,
            sender,
            [PriceObservation("1", snapshot("1", 1040))],
            feed=shop(price_min_change_amount=Decimal(100)),
        )

        # Then
        assert sender.messages == []
        assert persisted_amounts(store) == {"1": Decimal(1040)}
        health = store.list_health("feed")[0]
        assert (health.state, health.item_count) == ("healthy", 1)
        assert store.count_pending_price_notifications("feed") == 0


def test_mixed_scan_alerts_only_qualifying_changes_and_baselines_the_rest(
    tmp_path: Path,
) -> None:
    # Given one large change, one filtered change, and one new product
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, (snapshot("big", 1000), snapshot("tiny", 1000)))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        # When
        run(
            store,
            sender,
            [
                PriceObservation("big", snapshot("big", 800)),
                PriceObservation("tiny", snapshot("tiny", 1040)),
                PriceObservation("new", snapshot("new", 500)),
            ],
            feed=shop(price_min_change_amount=Decimal(100)),
        )

        # Then
        assert len(sender.messages) == 1
        assert sender.messages[0].entry.title == "Product big"
        assert persisted_amounts(store) == {
            "big": Decimal(800),
            "new": Decimal(500),
            "tiny": Decimal(1040),
        }


def test_suppressed_change_is_not_re_alerted_on_the_next_scan(
    tmp_path: Path,
) -> None:
    # Given a first scan that filters a 4% rise
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, (snapshot("1", 1000),))
        sender = RecordingSender([])
        run(
            store,
            sender,
            [PriceObservation("1", snapshot("1", 1040))],
            feed=shop(price_min_change_percentage=5),
        )

        # When the same price is observed again
        run(
            store,
            sender,
            [PriceObservation("1", snapshot("1", 1040))],
            feed=shop(price_min_change_percentage=5),
        )

        # Then nothing is re-evaluated or alerted
        assert sender.messages == []
        assert persisted_amounts(store) == {"1": Decimal(1040)}
        assert store.list_health("feed")[0].state == "healthy"


def test_inline_classification_uses_the_post_filter_count(tmp_path: Path) -> None:
    # Given three detected changes but only two above the filter
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(
            store,
            (
                snapshot("a", 1000),
                snapshot("b", 1000),
                snapshot("tiny", 1000),
            ),
        )
        seen: list[tuple[str, ...]] = []

        def confirm(
            observations: Sequence[PriceObservation[str]],
        ) -> Sequence[PriceObservation[str]]:
            seen.append(tuple(item.snapshot.product_id for item in observations))
            return observations

        sender = RecordingSender(
            [DiscordDeliveryResult.DELIVERED, DiscordDeliveryResult.DELIVERED],
        )

        # When the inline limit equals the alerting count, not the raw count
        run(
            store,
            sender,
            [
                PriceObservation("a", snapshot("a", 500)),
                PriceObservation("b", snapshot("b", 700)),
                PriceObservation("tiny", snapshot("tiny", 1030)),
            ],
            feed=shop(
                price_min_change_amount=Decimal(100),
                price_inline_limit=2,
            ),
            confirm=confirm,
        )

        # Then two single cards are sent and only alerting changes are confirmed
        assert [message.entry.title for message in sender.messages] == [
            "Product a",
            "Product b",
        ]
        assert seen == [("a", "b")]
        assert persisted_amounts(store) == {
            "a": Decimal(500),
            "b": Decimal(700),
            "tiny": Decimal(1030),
        }


def test_bulk_delivery_baselines_suppressed_snapshots_without_confirming(
    tmp_path: Path,
) -> None:
    # Given more alerting changes than the inline limit plus one filtered change
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(
            store,
            (
                snapshot("a", 1000),
                snapshot("b", 1000),
                snapshot("tiny", 1000),
            ),
        )
        seen: list[tuple[str, ...]] = []

        def confirm(
            observations: Sequence[PriceObservation[str]],
        ) -> Sequence[PriceObservation[str]]:
            seen.append(tuple(item.snapshot.product_id for item in observations))
            return observations

        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        # When
        run(
            store,
            sender,
            [
                PriceObservation("a", snapshot("a", 500)),
                PriceObservation("b", snapshot("b", 700)),
                PriceObservation("tiny", snapshot("tiny", 1030)),
            ],
            feed=shop(
                price_min_change_amount=Decimal(100),
                price_inline_limit=1,
            ),
            confirm=confirm,
            confirmation_limit=10,
            limit=500,
        )

        # Then one digest is sent, the filtered change never reaches confirmation,
        # and its snapshot still becomes the stored baseline
        assert len(sender.messages) == 1
        assert sender.messages[0].entry.title == "Price changes"
        assert seen == [("a", "b")]
        assert persisted_amounts(store) == {
            "a": Decimal(500),
            "b": Decimal(700),
            "tiny": Decimal(1030),
        }


def test_ignore_increases_suppresses_a_rise_and_delivers_a_drop(
    tmp_path: Path,
) -> None:
    # Given one rising and one falling price
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, (snapshot("rise", 1000), snapshot("drop", 1000)))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        # When
        run(
            store,
            sender,
            [
                PriceObservation("rise", snapshot("rise", 1100)),
                PriceObservation("drop", snapshot("drop", 900)),
            ],
            feed=shop(price_ignore_increases=True),
        )

        # Then only the drop is posted and both baselines move
        assert len(sender.messages) == 1
        assert sender.messages[0].entry.title == "Product drop"
        assert persisted_amounts(store) == {
            "rise": Decimal(1100),
            "drop": Decimal(900),
        }


def test_default_filters_deliver_every_change(tmp_path: Path) -> None:
    # Given the default (disabled) filters
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, (snapshot("1", 1000),))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        # When a tiny increase is detected
        run(
            store,
            sender,
            [PriceObservation("1", snapshot("1", 1001))],
            feed=shop(),
        )

        # Then it is still delivered
        assert len(sender.messages) == 1
        assert persisted_amounts(store) == {"1": Decimal(1001)}
