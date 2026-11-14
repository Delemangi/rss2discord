"""Real monitor-to-store recovery regressions for DDStore and Hivetec."""

from dataclasses import replace
from pathlib import Path
from time import time

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import (
    DiscordDeliveryResult,
    SleepCallback,
    WebhookMessage,
)
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import HealthUpdate
from rss2discord.providers.ddstore.prices import DDStorePriceMonitor
from rss2discord.providers.hivetec.prices import HivetecPriceMonitor
from rss2discord.retries import FeedFetchInterruptedError
from tests import test_ddstore_price_monitor as dd
from tests import test_hivetec_price_monitor as hive
from tests.setec_price_monitor_helpers import RecordingSender

PROVIDERS = (
    "anhoch",
    "neksio",
    "setec",
    "cccenter",
    "gjirafa50",
    "neptun",
    "pazar3",
    "reklama5",
    "technomarket",
    "ddstore",
    "hivetec",
)


def monitor_for(
    provider: str,
    batches: list[tuple[int, ...]],
    store: DeliveryStore,
    sender: RecordingSender,
) -> DDStorePriceMonitor | HivetecPriceMonitor:
    if provider == "ddstore":
        catalog = dd.CatalogStub(
            [
                tuple(
                    dd.make_product(str(i), amount=price)
                    for i, price in enumerate(batch, 1)
                )
                for batch in batches
            ],
        )
        return dd.make_monitor(dd.make_feed(), catalog, store, sender)
    catalog_h = hive.CatalogStub(
        [
            tuple(hive.product(i, str(price * 100)) for i, price in enumerate(batch, 1))
            for batch in batches
        ],
    )
    return hive.monitor(catalog_h, store, sender)


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
@pytest.mark.parametrize("checkpoint", [1, 2, 3])
def test_catalog_shutdown_before_planning_never_writes_or_sends(
    tmp_path: Path,
    provider: str,
    checkpoint: int,
) -> None:
    sender = RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = monitor_for(provider, [(100,)], store, sender)
        checks = iter([False] * (checkpoint - 1) + [True])
        monitor._dependencies = replace(
            monitor._dependencies,
            delivery=replace(
                monitor._dependencies.delivery,
                is_shutdown_requested=lambda: next(checks),
            ),
        )
        with pytest.raises(FeedFetchInterruptedError):
            monitor.scan()
        assert store.load_price_snapshots(provider) == ()
        assert store.list_health(provider) == ()
        assert sender.messages == []


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
def test_catalog_silent_updates_persist_atomically_with_digest(
    tmp_path: Path,
    provider: str,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        changed = (90,) * 101 + (100, 70)
        monitor = monitor_for(
            provider,
            [(100,) * 102, changed],
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        snapshots = {s.product_id: s for s in store.load_price_snapshots(provider)}
        assert len(snapshots) == 103
        assert snapshots["103"].amount == 70
        assert sum(s.amount == 90 for s in snapshots.values()) == 101
        assert len(sender.messages) == 1
        assert store.count_pending_price_notifications(provider) == 0


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
@pytest.mark.parametrize("count", [1, 100])
def test_normal_changes_send_without_approval_or_send_cap(
    tmp_path: Path,
    provider: str,
    count: int,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = monitor_for(provider, [(100,) * count, (90,) * count], store, sender)
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == 1
        assert all(p.amount == 90 for p in store.load_price_snapshots(provider))
        assert store.list_health(provider)[0].state == "healthy"


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
def test_actual_101_changes_deliver_one_detailed_digest(
    tmp_path: Path,
    provider: str,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = monitor_for(
            provider,
            [(100,) * 101, (90,) * 101],
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert message.entry.title == "Price changes"
        assert len(message.files) == 2
        assert all(p.amount == 90 for p in store.load_price_snapshots(provider))
        assert store.list_health(provider)[0].state == "healthy"


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
def test_failed_digest_retries_the_same_pending_rows(
    tmp_path: Path,
    provider: str,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        sender = RecordingSender(
            [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.DELIVERED],
        )
        monitor = monitor_for(
            provider,
            [(100,) * 101, (90,) * 101, (90,) * 101],
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        pending = store.load_pending_price_group(provider)
        assert pending is not None
        assert len(pending[1]) == 101
        assert all(item.attempts == 1 for item in pending[1])
        assert all(p.amount == 90 for p in store.load_price_snapshots(provider))
        monitor.scan()
        assert len(sender.messages) == 2
        assert (
            sender.messages[0].files[1].content == sender.messages[1].files[1].content
        )
        assert store.count_pending_price_notifications(provider) == 0


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
@pytest.mark.parametrize("failure", ["fetch", "challenge"])
def test_fetch_failure_raises_without_pausing_or_sending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    failure: str,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        sender = RecordingSender([])
        monitor = monitor_for(
            provider,
            [(100,) * 101, (90,) * 101],
            store,
            sender,
        )
        monitor.scan()

        def fail(*args: object, **kwargs: object) -> None:
            raise FeedFetchError(
                provider,
                "BotChallenge" if failure == "challenge" else "IncompleteCatalog",
            )

        monkeypatch.setattr(monitor._dependencies.catalog, "fetch_catalog", fail)
        with pytest.raises(FeedFetchError):
            monitor.scan()
        assert sender.messages == []


@pytest.mark.parametrize("provider", ["ddstore", "hivetec"])
def test_repeated_digest_failures_keep_rows_pending(
    tmp_path: Path,
    provider: str,
) -> None:
    scans = 5
    sender = RecordingSender([DiscordDeliveryResult.FAILED] * scans)
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = monitor_for(
            provider,
            [(100,) * 101, *([(90,) * 101] * scans)],
            store,
            sender,
        )
        monitor.scan()
        for _ in range(scans):
            monitor.scan()
        assert len(sender.messages) == scans
        pending = store.load_pending_price_group(provider)
        assert pending is not None
        assert len(pending[1]) == 101
        assert all(item.attempts == scans for item in pending[1])
        assert store.count_pending_price_notifications(provider) == 101
        assert all(p.amount == 90 for p in store.load_price_snapshots(provider))


def test_repeated_health_failure_transition_updates_without_binding_error(
    tmp_path: Path,
) -> None:
    now = int(time())
    with DeliveryStore(tmp_path / "state.db") as store:
        first = store.record_health(
            HealthUpdate(
                feed_id="feed",
                job_kind="price",
                state="failed",
                cause="SourceMoved",
                attempted_at=now,
                success=False,
                nonempty=False,
                item_count=0,
            ),
        )
        second = store.record_health(
            HealthUpdate(
                feed_id="feed",
                job_kind="price",
                state="failed",
                cause="SourceMoved",
                attempted_at=now + 1,
                success=False,
                nonempty=False,
                item_count=0,
            ),
        )
        assert first.changed
        assert not second.changed
        health = store.list_health("feed")[0]
        assert health.cause == "SourceMoved"
        assert health.total_attempts == 2


@pytest.mark.parametrize(
    "outcome",
    [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.INTERRUPTED, "exception"],
)
def test_unsuccessful_sender_keeps_outbox_rows_pending(
    tmp_path: Path,
    outcome: DiscordDeliveryResult | str,
) -> None:
    class UnsuccessfulSender(RecordingSender):
        def send(
            self,
            message: WebhookMessage,
            sleep: SleepCallback,
        ) -> DiscordDeliveryResult:
            if outcome == "exception":
                raise RuntimeError("fixture sender failed")
            return super().send(message, sleep)

    sender = UnsuccessfulSender(
        [outcome] if isinstance(outcome, DiscordDeliveryResult) else [],
    )
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = monitor_for(
            "ddstore",
            [(100,) * 101, (90,) * 101, (90,) * 101],
            store,
            sender,
        )
        monitor.scan()
        if outcome == "exception":
            with pytest.raises(RuntimeError, match="fixture sender failed"):
                monitor.scan()
        else:
            monitor.scan()
        pending = store.load_pending_price_group("ddstore")
        assert pending is not None
        assert len(pending[1]) == 101
        assert all(
            item.attempts == (1 if outcome is DiscordDeliveryResult.FAILED else 0)
            for item in pending[1]
        )
        assert all(p.amount == 90 for p in store.load_price_snapshots("ddstore"))
