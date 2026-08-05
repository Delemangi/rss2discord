from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports import FeedFetchError, pazar3_price_monitor
from rss2discord.transports.pazar3_models import Pazar3Listing
from rss2discord.transports.pazar3_price_monitor import (
    Pazar3PriceMonitor,
    Pazar3PriceMonitorDependencies,
)
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.setec_price_monitor_helpers import RecordingSender
from tests.test_pazar3_strategy import listing


class CatalogStub:
    def __init__(self, batches: list[tuple[Pazar3Listing, ...]]) -> None:
        self._batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Pazar3Listing, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._batches.pop(0)


def feed() -> FeedConfig:
    return FeedConfig(
        id="pazar3",
        name="Pazar3 Parts",
        url="https://www.pazar3.mk/oglasi/elektronika/delovi/prodazba",
        webhook="https://discord.test/webhook",
        strategy="pazar3",
    )


def priced(entry_id: str, price: str) -> Pazar3Listing:
    return replace(listing(entry_id, minutes=1), price=price)


def monitor(
    catalog: CatalogStub,
    store: DeliveryStore,
    sender: RecordingSender,
) -> Pazar3PriceMonitor:
    return Pazar3PriceMonitor(
        feed(),
        Pazar3PriceMonitorDependencies(
            catalog=catalog,
            snapshots=store,
            sender=sender,
            fetch_retry_policy=FetchRetryPolicy(
                sleep=lambda seconds: True,
                on_retry=lambda error, delay: None,
            ),
            sqlite_retry_policy=SQLiteRetryPolicy(
                sleep=lambda seconds: True,
                on_retry=lambda error, delay: None,
            ),
            delivery=PriceAlertDelivery(
                sleep=lambda seconds: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
        ),
    )


def test_pazar3_price_monitor_baselines_then_delivers_currency_change(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    catalog = CatalogStub(
        [
            (priced("1", "1.200 МКД"),),
            (priced("1", "20 EUR"),),
        ],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(catalog, store, sender)
        price_monitor.scan()
        assert sender.messages == []

        price_monitor.scan()

        assert sender.messages[0].entry.description == (
            "Price changed from 1.200 МКД to 20 EUR"
        )
        assert store.load_price_snapshots("pazar3") == (
            PriceSnapshot("pazar3", "1", Decimal(20), "20 EUR", "EUR"),
        )


def test_pazar3_price_monitor_ignores_unavailable_price(
    tmp_path: Path,
) -> None:
    catalog = CatalogStub(
        [
            (priced("1", "100 ден."),),
            (priced("1", "По договор"), priced("2", "0 EUR")),
        ],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(catalog, store, RecordingSender([]))
        price_monitor.scan()
        price_monitor.scan()

        assert store.load_price_snapshots("pazar3") == (
            PriceSnapshot("pazar3", "1", Decimal(100), "100 ден.", "MKD"),
        )


def test_pazar3_price_monitor_retries_failed_delivery_without_advancing_snapshot(
    tmp_path: Path,
) -> None:
    sender = RecordingSender(
        [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.DELIVERED],
    )
    changed = (priced("1", "90 МКД"),)
    catalog = CatalogStub([(priced("1", "100 МКД"),), changed, changed])

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(catalog, store, sender)
        price_monitor.scan()
        price_monitor.scan()
        assert store.load_price_snapshots("pazar3")[0].amount == Decimal(100)

        price_monitor.scan()

        assert len(sender.messages) == 2
        assert store.load_price_snapshots("pazar3")[0].amount == Decimal(90)


def test_pazar3_price_monitor_rejects_change_limit_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pazar3_price_monitor,
        "MAX_PAZAR3_PRICE_CHANGES_PER_SCAN",
        1,
    )
    baseline = (priced("1", "100 МКД"), priced("2", "200 EUR"))
    changed = (priced("1", "90 МКД"), priced("2", "190 EUR"))

    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(
            CatalogStub([baseline, changed]),
            store,
            RecordingSender([]),
        )
        price_monitor.scan()

        with pytest.raises(FeedFetchError) as fetch_error:
            price_monitor.scan()

        assert fetch_error.value.cause_type == "PriceChangeLimitExceeded"
        assert {
            snapshot.amount for snapshot in store.load_price_snapshots("pazar3")
        } == {
            Decimal(100),
            Decimal(200),
        }


def test_pazar3_price_monitor_updates_formatting_without_alert(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        price_monitor = monitor(
            CatalogStub(
                [
                    (priced("1", "1 200 МКД"),),
                    (priced("1", "1.200 МКД"),),
                ],
            ),
            store,
            RecordingSender([]),
        )
        price_monitor.scan()
        price_monitor.scan()

        assert store.load_price_snapshots("pazar3") == (
            PriceSnapshot("pazar3", "1", Decimal(1_200), "1.200 МКД", "MKD"),
        )


def test_pazar3_price_monitor_rejects_snapshot_limit_before_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pazar3_price_monitor, "MAX_PAZAR3_RETAINED_SNAPSHOTS", 1)
    snapshots = (
        PriceSnapshot("pazar3", "1", Decimal(100), "100 МКД", "MKD"),
        PriceSnapshot("pazar3", "2", Decimal(200), "200 EUR", "EUR"),
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshots(snapshots)
        price_monitor = monitor(CatalogStub([()]), store, RecordingSender([]))

        with pytest.raises(FeedFetchError) as fetch_error:
            price_monitor.scan()

        assert fetch_error.value.cause_type == "SnapshotLimitExceeded"
        assert store.load_price_snapshots("pazar3") == snapshots
