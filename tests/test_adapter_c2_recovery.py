from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.cccenter_models import CCCenterListing, CCCenterProduct
from rss2discord.transports.cccenter_price_monitor import (
    CCCenterPriceMonitor,
    CCCenterPriceMonitorDependencies,
)
from rss2discord.transports.price_monitor import PriceAlertDelivery
from rss2discord.transports.setec_models import SetecPriceEntry
from rss2discord.transports.setec_price_monitor import SetecPriceMonitor
from tests import setec_price_monitor_helpers as setec
from tests.test_cccenter_price_monitor import CatalogStub, product


class DetailCatalog(CatalogStub):
    def __init__(
        self,
        batches: list[tuple[CCCenterProduct, ...]],
        override: CCCenterProduct | None = None,
    ) -> None:
        super().__init__(batches)
        self.calls: list[tuple[str, ...]] = []
        self.override = override

    def fetch_product_details(
        self,
        listings: Sequence[CCCenterListing | CCCenterProduct],
        *,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[CCCenterProduct, ...]:
        self.calls.append(tuple(item.product_id for item in listings))
        if self.override is not None:
            return (self.override,)
        return super().fetch_product_details(
            listings,
            is_shutdown_requested=is_shutdown_requested,
        )


def cc_monitor(
    store: DeliveryStore,
    catalog: DetailCatalog,
    sender: setec.RecordingSender,
) -> CCCenterPriceMonitor:
    return CCCenterPriceMonitor(
        FeedConfig(
            id="cccenter",
            url="https://cccenter.mk/shop/",
            webhook="https://discord.example.test/webhook",
            strategy="cccenter",
        ),
        CCCenterPriceMonitorDependencies(
            catalog=catalog,
            snapshots=store,
            sender=sender,
            fetch_retry_policy=FetchRetryPolicy(
                sleep=lambda _: True,
                on_retry=lambda *_: None,
            ),
            sqlite_retry_policy=SQLiteRetryPolicy(
                sleep=lambda _: True,
                on_retry=lambda *_: None,
            ),
            delivery=PriceAlertDelivery(
                sleep=lambda _: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
        ),
    )


@pytest.mark.parametrize("provider", ["setec", "cccenter"])
@pytest.mark.parametrize(
    "outcome",
    [DiscordDeliveryResult.DELIVERED, DiscordDeliveryResult.FAILED],
)
def test_true_101_change_quarantine_approval_bounded_drain_and_rotation(
    tmp_path: Path,
    provider: str,
    outcome: DiscordDeliveryResult,
) -> None:
    sender = setec.RecordingSender([outcome] * 20)
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor: SetecPriceMonitor | CCCenterPriceMonitor
        if provider == "setec":
            baseline = tuple(
                setec.make_product(str(i), calculated_amount=100) for i in range(101)
            )
            changed = tuple(
                setec.make_product(str(i), calculated_amount=90) for i in range(101)
            )
            catalog = setec.CatalogStub([baseline, changed, changed, changed])
            monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
            calls = catalog.requested_id_batches
        else:
            cc_baseline = tuple(
                product("100", f"https://cccenter.mk/product/{i}/") for i in range(101)
            )
            cc_changed = tuple(
                replace(p, current_price=Decimal(90)) for p in cc_baseline
            )
            cc_catalog = DetailCatalog(
                [cc_baseline, cc_changed, cc_changed, cc_changed],
            )
            monitor = cc_monitor(store, cc_catalog, sender)
            calls = cc_catalog.calls
        monitor.scan()
        monitor.scan()
        assert not sender.messages
        assert not calls
        assert all(p.amount == 100 for p in store.load_price_snapshots(provider))
        candidate = store.list_price_change_batches(feed_id=provider)[0]
        store.approve_price_change_batch(
            feed_id=provider,
            fingerprint=candidate.fingerprint,
            reason="fixture review",
        )
        monitor.scan()
        assert len(sender.messages) == 10
        batch = store.load_active_price_batch(provider)
        assert batch is not None
        assert batch.pending_count == (
            91 if outcome is DiscordDeliveryResult.DELIVERED else 101
        )
        assert sum(item.attempt_count for item in batch.items) == 10
        monitor.scan()
        assert len(sender.messages) == 20
        assert len(calls) == 2
        assert all(len(ids) == 10 for ids in calls)
        assert not set(calls[0]) & set(calls[1])


@pytest.mark.parametrize(
    "detail",
    [product("95"), product(None), replace(product("90"), price_status="variable")],
)
def test_cccenter_detail_disagreement_defers_without_snapshot_mutation(
    tmp_path: Path,
    detail: CCCenterProduct,
) -> None:
    sender = setec.RecordingSender([])
    catalog = DetailCatalog([(product("100"),), (product("90"),)], detail)
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = cc_monitor(store, catalog, sender)
        monitor.scan()
        monitor.scan()
        assert not sender.messages
        assert store.load_price_snapshots("cccenter")[0].amount == 100
        assert len(catalog.calls) == 1


def test_cccenter_confirmed_detail_delivers_once(tmp_path: Path) -> None:
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    catalog = DetailCatalog([(product("100"),), (product("90"),), (product("90"),)])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = cc_monitor(store, catalog, sender)
        for _ in range(3):
            monitor.scan()
        assert len(sender.messages) == len(catalog.calls) == 1
        assert store.load_price_snapshots("cccenter")[0].amount == 90


@pytest.mark.parametrize("mutation", ["target", "previous", "missing"])
def test_setec_validates_entire_pending_manifest_and_keeps_pause(
    tmp_path: Path,
    mutation: str,
) -> None:
    baseline = tuple(
        setec.make_product(str(i), calculated_amount=100) for i in range(101)
    )
    changed = tuple(
        setec.make_product(str(i), calculated_amount=90) for i in range(101)
    )
    invalid = changed
    if mutation == "target":
        invalid = (*changed[:-1], setec.make_product("100", calculated_amount=80))
    elif mutation == "missing":
        invalid = changed[:-1]
    catalog = setec.CatalogStub([baseline, changed, invalid, changed])
    sender = setec.RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()
        candidate = store.list_price_change_batches(feed_id="setec")[0]
        store.approve_price_change_batch(
            feed_id="setec",
            fingerprint=candidate.fingerprint,
            reason="fixture review",
        )
        if mutation == "previous":
            old = next(
                p for p in store.load_price_snapshots("setec") if p.product_id == "100"
            )
            store.upsert_price_snapshot(replace(old, amount=Decimal(99)))
        monitor.scan()
        monitor.scan()
        batch = store.load_active_price_batch("setec")
        assert batch is not None
        assert batch.status == "paused"
        assert not sender.messages
        assert not catalog.requested_id_batches


def test_cccenter_rejects_detail_source_identity_change(tmp_path: Path) -> None:
    sender = setec.RecordingSender([])
    catalog = DetailCatalog(
        [(product("100"),), (product("90"),)],
        product("90", "https://cccenter.mk/product/other/"),
    )
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = cc_monitor(store, catalog, sender)
        monitor.scan()
        with pytest.raises(FeedFetchError, match="InvalidProductIdentity"):
            monitor.scan()
        assert store.load_price_snapshots("cccenter")[0].amount == 100


def test_approved_setec_full_drain_defers_outside_changes_and_silent_writes(
    tmp_path: Path,
) -> None:
    base = tuple(setec.make_product(str(i), calculated_amount=100) for i in range(101))
    targets = tuple(
        setec.make_product(str(i), calculated_amount=90) for i in range(101)
    )
    unchanged = setec.make_product("outside", calculated_amount=100)
    live = (
        *targets,
        setec.make_product("outside", calculated_amount=80),
        setec.make_product("fresh", calculated_amount=70),
    )
    catalog = setec.CatalogStub(
        [(*base, unchanged), (*targets, unchanged), *([live] * 12)],
    )
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED] * 102)
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()
        candidate = store.list_price_change_batches(feed_id="setec")[0]
        store.approve_price_change_batch(
            feed_id="setec",
            fingerprint=candidate.fingerprint,
            reason="fixture review",
        )
        for _ in range(11):
            monitor.scan()
        assert len(sender.messages) == 101
        assert store.load_active_price_batch("setec") is None
        persisted = {p.product_id: p for p in store.load_price_snapshots("setec")}
        assert persisted["outside"].amount == 100
        assert "fresh" not in persisted
        completed = store.list_price_change_batches(
            feed_id="setec",
            include_terminal=True,
        )[0]
        assert completed.fingerprint == candidate.fingerprint
        assert completed.status == "completed"
        monitor.scan()
        assert len(sender.messages) == 102
        persisted = {p.product_id: p for p in store.load_price_snapshots("setec")}
        assert persisted["outside"].amount == 80
        assert persisted["fresh"].amount == 70


@pytest.mark.parametrize("mismatch", ["listing", "detail", "currency"])
def test_cccenter_approved_inconsistency_pauses_before_any_send(
    tmp_path: Path,
    mismatch: str,
) -> None:
    baseline = tuple(
        product("100", f"https://cccenter.mk/product/{i}/") for i in range(101)
    )
    targets = tuple(replace(p, current_price=Decimal(90)) for p in baseline)
    live = (
        targets
        if mismatch != "listing"
        else (*targets[:-1], replace(targets[-1], current_price=Decimal(80)))
    )
    catalog = DetailCatalog([baseline, targets, live, targets])
    sender = setec.RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = cc_monitor(store, catalog, sender)
        monitor.scan()
        monitor.scan()
        candidate = store.list_price_change_batches(feed_id="cccenter")[0]
        store.approve_price_change_batch(
            feed_id="cccenter",
            fingerprint=candidate.fingerprint,
            reason="fixture review",
        )
        if mismatch == "detail":
            catalog.override = replace(targets[0], current_price=Decimal(95))
        if mismatch == "currency":
            prior = store.load_price_snapshots("cccenter")[-1]
            store.upsert_price_snapshot(replace(prior, currency="EUR"))
        monitor.scan()
        monitor.scan()
        batch = store.load_active_price_batch("cccenter")
        assert batch is not None
        assert batch.status == "paused"
        assert not sender.messages
        assert all(p.amount == 100 for p in store.load_price_snapshots("cccenter"))
        assert len(catalog.calls) == (1 if mismatch == "detail" else 0)


@pytest.mark.parametrize(
    "cause",
    ["ProductLimitExceeded", "InvalidCurrency", "IncompleteCatalog"],
)
def test_hard_catalog_failure_pauses_approved_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cause: str,
) -> None:
    baseline = tuple(
        setec.make_product(str(i), calculated_amount=100) for i in range(101)
    )
    changed = tuple(
        setec.make_product(str(i), calculated_amount=90) for i in range(101)
    )
    catalog = setec.CatalogStub([baseline, changed])
    sender = setec.RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()
        candidate = store.list_price_change_batches(feed_id="setec")[0]
        store.approve_price_change_batch(
            feed_id="setec",
            fingerprint=candidate.fingerprint,
            reason="fixture review",
        )

        def fail(
            url: str,
            *,
            retry_policy: FetchRetryPolicy,
            is_shutdown_requested: Callable[[], bool],
        ) -> tuple[SetecPriceEntry, ...]:
            del url, retry_policy, is_shutdown_requested
            raise FeedFetchError("Setec", cause)

        monkeypatch.setattr(catalog, "fetch_price_index", fail)
        monitor.scan()
        batch = store.load_active_price_batch("setec")
        assert batch is not None
        assert batch.status == "paused"
        assert not sender.messages
        assert not catalog.requested_id_batches
        assert all(
            snapshot.amount == 100 for snapshot in store.load_price_snapshots("setec")
        )


@pytest.mark.parametrize("provider", ["setec", "cccenter"])
@pytest.mark.parametrize("revoke_at", ["detail", "spacing"])
def test_revoke_before_jit_claim_prevents_next_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    revoke_at: str,
) -> None:
    path = tmp_path / "state.db"
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(path) as store, DeliveryStore(path) as admin:
        monitor: SetecPriceMonitor | CCCenterPriceMonitor
        if provider == "setec":
            before = tuple(
                setec.make_product(str(i), calculated_amount=100) for i in range(101)
            )
            after = tuple(
                setec.make_product(str(i), calculated_amount=90) for i in range(101)
            )
            catalog = setec.CatalogStub([before, after, after])
            monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        else:
            cc_before = tuple(
                product("100", f"https://cccenter.mk/product/{i}/") for i in range(101)
            )
            cc_after = tuple(replace(p, current_price=Decimal(90)) for p in cc_before)
            cc_catalog = DetailCatalog([cc_before, cc_after, cc_after])
            monitor = cc_monitor(store, cc_catalog, sender)
        monitor.scan()
        monitor.scan()
        candidate = store.list_price_change_batches(provider)[0]
        store.approve_price_change_batch(
            feed_id=provider,
            fingerprint=candidate.fingerprint,
            reason="reviewed",
        )

        def revoke() -> None:
            batch = admin.load_active_price_batch(provider)
            assert batch is not None
            assert sum(item.attempt_count for item in batch.items) == (
                0 if revoke_at == "detail" else 1
            )
            admin.revoke_price_change_batch(
                batch.batch_id,
                batch.fingerprint,
                "fixture revocation",
            )

        if revoke_at == "detail":
            if isinstance(monitor, SetecPriceMonitor):
                resolve = monitor._resolve_changes

                def resolve_and_revoke(*args: object, **kwargs: object) -> object:
                    result = resolve(*args, **kwargs)  # type: ignore[arg-type]
                    revoke()
                    return result

                monkeypatch.setattr(monitor, "_resolve_changes", resolve_and_revoke)
            else:
                confirm = monitor._confirm_changes

                def confirm_and_revoke(*args: object, **kwargs: object) -> object:
                    result = confirm(*args, **kwargs)  # type: ignore[arg-type]
                    revoke()
                    return result

                monkeypatch.setattr(monitor, "_confirm_changes", confirm_and_revoke)
        else:

            def sleep_and_revoke(_seconds: float) -> bool:
                revoke()
                return True

            monitor._dependencies = replace(
                monitor._dependencies,
                delivery=PriceAlertDelivery(sleep_and_revoke, 1, lambda: False),
            )
        monitor.scan()
        batch = store.load_price_batch(candidate.batch_id)
        assert batch is not None
        expected = 0 if revoke_at == "detail" else 1
        assert len(sender.messages) == batch.delivered_count == expected
        assert sum(item.attempt_count for item in batch.items) == expected
        assert batch.status == "revoked"
