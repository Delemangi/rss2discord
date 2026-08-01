from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import SourceMetric
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports import FeedFetchError, neptun_price_monitor
from rss2discord.transports.neptun_models import NeptunProduct
from rss2discord.transports.neptun_price_monitor import (
    NeptunPriceMonitor,
    NeptunPriceMonitorDependencies,
)
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.neptun_helpers import product_payload
from tests.setec_price_monitor_helpers import RecordingSender


class CatalogStub:
    def __init__(self, batches: list[tuple[NeptunProduct, ...]]) -> None:
        self.batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[NeptunProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        return self.batches.pop(0)


def make_product(product_id: int, price: Decimal | int) -> NeptunProduct:
    return NeptunProduct.model_validate(
        product_payload(product_id, actual_price=price),
    )


def make_feed() -> FeedConfig:
    return FeedConfig(
        id="neptun",
        name="Neptun Computers",
        url="https://www.neptun.mk/KOMPJUTERI.nspx",
        webhook="https://discord.test/webhooks/id/token",
        strategy="neptun",
    )


def make_monitor(
    catalog: CatalogStub,
    store: DeliveryStore,
    sender: RecordingSender,
) -> NeptunPriceMonitor:
    return NeptunPriceMonitor(
        make_feed(),
        NeptunPriceMonitorDependencies(
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


def test_price_monitor_silently_baselines_then_delivers_actual_price_change(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    catalog = CatalogStub([(make_product(1, 100),), (make_product(1, 90),)])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(catalog, store, sender)
        monitor.scan()
        monitor.scan()

        assert len(sender.messages) == 1
        assert sender.messages[0].entry.description == "Price decreased from 100 ден. to 90 ден."
        assert sender.messages[0].entry.source_metrics[:2] == (
            SourceMetric("Price", "90 ден."),
            SourceMetric("Previous", "100 ден."),
        )
        assert store.load_price_snapshots("neptun")[0].amount == Decimal(90)
        assert store.load_price_snapshots("neptun")[0].currency == "MKD"


def test_price_monitor_preserves_last_real_price_while_unavailable(tmp_path: Path) -> None:
    sender = RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        snapshot = PriceSnapshot("neptun", "1", Decimal(100), "100 ден.", "MKD")
        store.upsert_price_snapshot(snapshot)

        make_monitor(CatalogStub([(make_product(1, 0),)]), store, sender).scan()

        assert store.load_price_snapshots("neptun") == (snapshot,)
        assert sender.messages == []


@pytest.mark.parametrize(
    "outcome",
    [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.INTERRUPTED],
)
def test_price_monitor_advances_changed_snapshot_only_after_delivery(
    tmp_path: Path,
    outcome: DiscordDeliveryResult,
) -> None:
    sender = RecordingSender([outcome])
    catalog = CatalogStub([(make_product(1, 100),), (make_product(1, 90),)])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(catalog, store, sender)
        monitor.scan()
        monitor.scan()

        assert store.load_price_snapshots("neptun")[0].amount == Decimal(100)


def test_price_monitor_rejects_more_than_one_hundred_changes_without_mutation(
    tmp_path: Path,
) -> None:
    baseline = tuple(make_product(index, 100) for index in range(1, 102))
    changed = tuple(make_product(index, 90) for index in range(1, 102))
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED] * 101)

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(CatalogStub([baseline, changed]), store, sender)
        monitor.scan()

        with pytest.raises(FeedFetchError, match="PriceChangeLimitExceeded"):
            monitor.scan()

        assert sender.messages == []
        assert {snapshot.amount for snapshot in store.load_price_snapshots("neptun")} == {Decimal(100)}


def test_price_monitor_rejects_snapshot_capacity_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(neptun_price_monitor, "MAX_NEPTUN_RETAINED_SNAPSHOTS", 1)
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(
            PriceSnapshot("neptun", "old", Decimal(100), "100 ден.", "MKD"),
        )

        with pytest.raises(FeedFetchError, match="SnapshotLimitExceeded"):
            make_monitor(
                CatalogStub([(make_product(2, 200),)]),
                store,
                RecordingSender([]),
            ).scan()

        assert [snapshot.product_id for snapshot in store.load_price_snapshots("neptun")] == ["old"]


def test_price_monitor_persists_silent_additions_but_not_interrupted_changes(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.INTERRUPTED])
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(
            PriceSnapshot("neptun", "1", Decimal(100), "100 ден.", "MKD"),
        )

        make_monitor(
            CatalogStub([((make_product(1, 90)), make_product(2, 200))]),
            store,
            sender,
        ).scan()

        snapshots = {
            snapshot.product_id: snapshot
            for snapshot in store.load_price_snapshots("neptun")
        }
        assert snapshots["1"].amount == Decimal(100)
        assert snapshots["2"].amount == Decimal(200)


def test_price_monitor_retries_failed_alert_against_unchanged_snapshot(
    tmp_path: Path,
) -> None:
    sender = RecordingSender(
        [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.DELIVERED],
    )
    changed = (make_product(1, 90),)
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(
            PriceSnapshot("neptun", "1", Decimal(100), "100 ден.", "MKD"),
        )
        monitor = make_monitor(CatalogStub([changed, changed]), store, sender)

        monitor.scan()
        monitor.scan()

        assert len(sender.messages) == 2
        assert store.load_price_snapshots("neptun")[0].amount == Decimal(90)
