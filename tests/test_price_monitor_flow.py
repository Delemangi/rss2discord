from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports import price_monitor
from rss2discord.transports.price_monitor import (
    catalog,
    comparison,
    contracts,
    delivery,
)


def test_public_exports_share_stage_identities() -> None:
    assert price_monitor.PriceDeliveryPlan is contracts.PriceDeliveryPlan
    assert price_monitor.PriceAlertDelivery is contracts.PriceAlertDelivery
    assert price_monitor.diff_price_snapshots is comparison.diff_price_snapshots
    assert price_monitor.deliver_price_changes is delivery.deliver_price_changes
    assert (
        price_monitor.deliver_catalog_price_changes
        is catalog.deliver_catalog_price_changes
    )


def test_catalog_converts_in_observation_order_before_shutdown() -> None:
    previous = PriceSnapshot("feed", "1", Decimal(100), "100", "MKD")
    persisted = {key: replace(previous, product_id=key) for key in ("1", "2", "held")}
    current = {
        key: replace(persisted[key], amount=Decimal(90)) for key in ("2", "held", "1")
    }
    store = Mock()
    store.held_price_product_ids.return_value = frozenset({"held"})
    dependencies = Mock(snapshots=store)
    dependencies.delivery.is_shutdown_requested.return_value = True
    conversions: list[tuple[PriceSnapshot, PriceSnapshot]] = []

    def change_for(old: PriceSnapshot, new: PriceSnapshot) -> SimpleNamespace:
        conversions.append((old, new))
        return SimpleNamespace(previous=old, current=new)

    message_for = Mock()
    with pytest.raises(FeedFetchInterruptedError):
        price_monitor.deliver_catalog_price_changes(
            dependencies,
            message_for,
            change_for=change_for,
            feed_id="feed",
            provider="provider",
            current=current,
            persisted=persisted,
            catalog_count=3,
            operation_started_at=0,
        )

    assert conversions == [(persisted[key], current[key]) for key in ("2", "1")]
    dependencies.sqlite_retry_policy.execute.assert_not_called()
    dependencies.sender.send.assert_not_called()


@pytest.mark.parametrize(
    "result",
    [
        DiscordDeliveryResult.FAILED,
        DiscordDeliveryResult.INTERRUPTED,
        KeyboardInterrupt(),
    ],
)
def test_unsuccessful_approved_delivery_releases_without_acknowledging(
    result: DiscordDeliveryResult | BaseException,
) -> None:
    snapshot = PriceSnapshot("feed", "1", Decimal(90), "90", "MKD")
    store = Mock()
    dependencies = Mock(snapshots=store)
    dependencies.delivery.is_shutdown_requested.return_value = False
    dependencies.sqlite_retry_policy.execute.side_effect = lambda operation: operation()
    if isinstance(result, BaseException):
        dependencies.sender.send.side_effect = result
    else:
        dependencies.sender.send.return_value = result

    def run() -> None:
        price_monitor.deliver_price_changes(
            [SimpleNamespace(current=snapshot)],
            dependencies,
            Mock(),
            plan=price_monitor.PriceDeliveryPlan(("1",), batch_id=7),
        )

    if isinstance(result, BaseException):
        with pytest.raises(KeyboardInterrupt):
            run()
    else:
        run()

    store.claim_price_delivery_attempt.assert_called_once_with(7, "1")
    store.release_price_delivery_attempt.assert_called_once_with(
        store.claim_price_delivery_attempt.return_value,
    )
    store.record_approved_price_delivery.assert_not_called()
    store.upsert_price_snapshot.assert_not_called()
    dependencies.delivery.sleep.assert_not_called()


def test_only_successful_delivery_delays_the_next_attempt() -> None:
    snapshot = PriceSnapshot("feed", "1", Decimal(90), "90", "MKD")
    store = Mock()
    dependencies = Mock(snapshots=store)
    dependencies.delivery = price_monitor.PriceAlertDelivery(
        Mock(return_value=True),
        2,
        lambda: False,
    )
    dependencies.sqlite_retry_policy.execute.side_effect = lambda operation: operation()
    dependencies.sender.send.side_effect = [
        DiscordDeliveryResult.FAILED,
        DiscordDeliveryResult.DELIVERED,
        DiscordDeliveryResult.DELIVERED,
    ]
    price_monitor.deliver_price_changes(
        [
            SimpleNamespace(current=replace(snapshot, product_id=str(i)))
            for i in range(3)
        ],
        dependencies,
        Mock(),
        plan=price_monitor.PriceDeliveryPlan(("0", "1", "2")),
    )

    dependencies.delivery.sleep.assert_called_once_with(2)
    assert store.upsert_price_snapshot.call_count == 2
