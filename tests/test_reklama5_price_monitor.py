from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import EntryId, SourceMetric
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports import FeedFetchError, reklama5_price_monitor
from rss2discord.transports import reklama5 as reklama5_transport
from rss2discord.transports.price_monitor import PriceAlertDelivery
from rss2discord.transports.reklama5 import Reklama5Listing
from tests.reklama5_helpers import FIXED_NOW, SEARCH_URL
from tests.setec_price_monitor_helpers import RecordingSender


class CatalogStub:
    def __init__(self, batches: list[tuple[Reklama5Listing, ...]]) -> None:
        self._batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Reklama5Listing, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._batches.pop(0)


def _listing(entry_id: str, price: str) -> Reklama5Listing:
    return Reklama5Listing(
        entry_id=EntryId(entry_id),
        url=f"https://reklama5.mk/AdDetails?ad={entry_id}",
        title=f"Listing {entry_id}",
        summary="Summary",
        price=price,
        location="Скопје",
        category="Компјутери",
        activity_at=FIXED_NOW,
        image_url=None,
    )


def _feed() -> FeedConfig:
    return FeedConfig(
        id="reklama5",
        name="Reklama5 Computer Parts",
        url=SEARCH_URL,
        webhook="https://discord.test/webhook",
        strategy="reklama5",
    )


def _monitor(
    catalog: CatalogStub,
    store: DeliveryStore,
    sender: RecordingSender,
) -> reklama5_transport.Reklama5PriceMonitor:
    return reklama5_transport.Reklama5PriceMonitor(
        _feed(),
        reklama5_transport.Reklama5PriceMonitorDependencies(
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


def test_reklama5_price_monitor_silently_baselines_then_delivers_change(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    catalog = CatalogStub(
        [
            (_listing("1", "1.200 МКД"),),
            (_listing("1", "900 МКД"),),
        ],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = _monitor(catalog, store, sender)
        monitor.scan()
        assert sender.messages == []

        monitor.scan()

        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert message.entry.description == (
            "Price decreased from 1.200 МКД to 900 МКД"
        )
        assert message.entry.source_metrics[:2] == (
            SourceMetric("Price", "900 МКД"),
            SourceMetric("Previous", "1.200 МКД"),
        )
        assert store.load_price_snapshots("reklama5") == (
            PriceSnapshot("reklama5", "1", Decimal(900), "900 МКД", "MKD"),
        )


def test_reklama5_price_monitor_ignores_unavailable_prices_and_retains_baseline(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    catalog = CatalogStub(
        [
            (_listing("1", "100 ден."),),
            (
                replace(_listing("1", "100 ден."), price="По договор"),
                _listing("2", "100 EUR"),
                _listing("3", "0 ден."),
            ),
            (_listing("1", "90 ден."),),
        ],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = _monitor(catalog, store, sender)
        monitor.scan()
        monitor.scan()

        assert store.load_price_snapshots("reklama5") == (
            PriceSnapshot("reklama5", "1", Decimal(100), "100 ден.", "MKD"),
        )
        assert sender.messages == []

        monitor.scan()

        assert len(sender.messages) == 1
        assert sender.messages[0].entry.description == (
            "Price decreased from 100 ден. to 90 ден."
        )


def test_reklama5_price_monitor_retries_failed_change_without_advancing_snapshot(
    tmp_path: Path,
) -> None:
    sender = RecordingSender(
        [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.DELIVERED],
    )
    changed = (_listing("1", "90 ден."),)
    catalog = CatalogStub([(_listing("1", "100 ден."),), changed, changed])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = _monitor(catalog, store, sender)
        monitor.scan()
        monitor.scan()

        assert store.load_price_snapshots("reklama5")[0].amount == Decimal(100)

        monitor.scan()

        assert len(sender.messages) == 2
        assert store.load_price_snapshots("reklama5")[0].amount == Decimal(90)


def test_reklama5_price_monitor_parses_nonbreaking_thousands_separator(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        _monitor(
            CatalogStub([(_listing("1", "1\u00a0200 ден."),)]),
            store,
            RecordingSender([]),
        ).scan()

        assert store.load_price_snapshots("reklama5")[0].amount == Decimal(1_200)


def test_reklama5_price_monitor_rejects_change_limit_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reklama5_price_monitor,
        "MAX_REKLAMA5_PRICE_CHANGES_PER_SCAN",
        1,
    )
    baseline = (_listing("1", "100 ден."), _listing("2", "200 ден."))
    changed = (_listing("1", "90 ден."), _listing("2", "190 ден."))

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = _monitor(CatalogStub([baseline, changed]), store, RecordingSender([]))
        monitor.scan()

        with pytest.raises(FeedFetchError) as fetch_error:
            monitor.scan()

        assert fetch_error.value.cause_type == "PriceChangeLimitExceeded"
        assert {
            snapshot.amount for snapshot in store.load_price_snapshots("reklama5")
        } == {
            Decimal(100),
            Decimal(200),
        }
