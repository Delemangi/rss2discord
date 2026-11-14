from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.transports import price_monitor
from rss2discord.transports.price_monitor import (
    comparison,
    contracts,
    delivery,
    scan,
)


def test_public_exports_share_stage_identities() -> None:
    assert price_monitor.PriceDeliveryPlan is contracts.PriceDeliveryPlan
    assert price_monitor.PriceAlertDelivery is contracts.PriceAlertDelivery
    assert price_monitor.diff_price_snapshots is comparison.diff_price_snapshots
    assert price_monitor.deliver_price_changes is delivery.deliver_price_changes
    assert price_monitor.run_price_scan is scan.run_price_scan
    assert price_monitor.PriceObservation is scan.PriceObservation


@pytest.mark.parametrize(
    "result",
    [
        DiscordDeliveryResult.FAILED,
        DiscordDeliveryResult.INTERRUPTED,
        KeyboardInterrupt(),
    ],
)
def test_unsuccessful_delivery_does_not_persist_the_snapshot(
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
            plan=price_monitor.PriceDeliveryPlan(("1",)),
        )

    if isinstance(result, BaseException):
        with pytest.raises(KeyboardInterrupt):
            run()
    else:
        run()

    store.upsert_price_snapshot.assert_not_called()
    store.upsert_price_snapshots.assert_not_called()
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
