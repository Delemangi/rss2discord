"""Setec and CCCenter price-path regressions after the outbox refactor."""

import csv
import io
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import PriceSnapshot
from rss2discord.providers.cccenter.models import CCCenterListing, CCCenterProduct
from rss2discord.providers.cccenter.prices import (
    CCCenterCatalog,
    CCCenterPriceMonitor,
)
from rss2discord.providers.setec.models import SetecPriceEntry
from rss2discord.providers.setec.prices import SetecPriceMonitor
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceMonitorDependencies,
)
from tests import setec_price_monitor_helpers as setec
from tests.test_cccenter_price_monitor import CatalogStub, product

CCCenterPriceMonitorDependencies = PriceMonitorDependencies[CCCenterCatalog]


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


def cc_products(
    amount: str,
    ids: range = range(101),
) -> tuple[CCCenterProduct, ...]:
    return tuple(
        product(amount, f"https://cccenter.mk/product/{index}/") for index in ids
    )


@pytest.mark.parametrize("provider", ["setec", "cccenter"])
@pytest.mark.parametrize(
    "outcome",
    [DiscordDeliveryResult.DELIVERED, DiscordDeliveryResult.FAILED],
)
def test_bulk_change_delivers_digest_without_approval_or_batches(
    tmp_path: Path,
    provider: str,
    outcome: DiscordDeliveryResult,
) -> None:
    sender = setec.RecordingSender([outcome])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor: SetecPriceMonitor | CCCenterPriceMonitor
        if provider == "setec":
            baseline = tuple(
                setec.make_product(str(i), calculated_amount=100) for i in range(101)
            )
            changed = tuple(
                setec.make_product(str(i), calculated_amount=90) for i in range(101)
            )
            catalog = setec.CatalogStub([baseline, changed, changed])
            monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        else:
            cc_baseline = cc_products("100")
            cc_changed = tuple(
                replace(item, current_price=Decimal(90)) for item in cc_baseline
            )
            cc_catalog = DetailCatalog([cc_baseline, cc_changed, cc_changed])
            monitor = cc_monitor(store, cc_catalog, sender)
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert message.entry.title == "Price changes"
        assert len(message.files) == 2
        expected_changes = 100 if provider == "cccenter" else 101
        assert (
            sum(item.amount == 90 for item in store.load_price_snapshots(provider))
            == expected_changes
        )
        pending = store.count_pending_price_notifications(provider)
        assert pending == (
            expected_changes if outcome is DiscordDeliveryResult.FAILED else 0
        )


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


def test_setec_bulk_digest_excludes_unconfirmed_details(tmp_path: Path) -> None:
    baseline = tuple(
        setec.make_product(str(i), calculated_amount=100) for i in range(101)
    )
    changed = tuple(
        setec.make_product(str(i), calculated_amount=90) for i in range(101)
    )
    overrides = {
        item.id: setec.make_product(item.id, calculated_amount=80)
        for item in changed[:50]
    }
    catalog = setec.CatalogStub(
        [baseline, changed, changed],
        display_overrides=overrides,
    )
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == 1
        rows = list(
            csv.reader(io.StringIO(sender.messages[0].files[1].content.decode())),
        )
        assert len(rows) == 52
        assert (
            sum(item.amount == 90 for item in store.load_price_snapshots("setec")) == 51
        )


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


def test_setec_digest_baseline_is_atomic_and_later_change_sends_inline(
    tmp_path: Path,
) -> None:
    baseline = tuple(
        setec.make_product(str(index), calculated_amount=100) for index in range(101)
    )
    changed = tuple(
        setec.make_product(str(index), calculated_amount=90) for index in range(101)
    )
    outside = setec.make_product("outside", calculated_amount=100)
    fresh = setec.make_product("fresh", calculated_amount=70)
    live = (
        *changed,
        setec.make_product("outside", calculated_amount=80),
        fresh,
    )
    catalog = setec.CatalogStub(
        [(*baseline, outside, fresh), (*changed, outside, fresh), live],
    )
    sender = setec.RecordingSender(
        [DiscordDeliveryResult.DELIVERED, DiscordDeliveryResult.DELIVERED],
    )
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()
        persisted = {p.product_id: p for p in store.load_price_snapshots("setec")}
        assert persisted["outside"].amount == 100
        assert persisted["fresh"].amount == 70
        assert len(sender.messages) == 1
        monitor.scan()
        assert len(sender.messages) == 2
        assert sender.messages[1].entry.title == "Product outside"
        persisted = {p.product_id: p for p in store.load_price_snapshots("setec")}
        assert persisted["outside"].amount == 80


def test_currency_change_raises_without_pausing(tmp_path: Path) -> None:
    sender = setec.RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(
            PriceSnapshot("setec", "prod-1", Decimal(100), "100 ден.", "EUR"),
        )
        catalog = setec.CatalogStub(
            [(setec.make_product("prod-1", calculated_amount=90),)],
        )
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        with pytest.raises(FeedFetchError, match="CurrencyChanged"):
            monitor.scan()
        assert not sender.messages
        assert store.list_health("setec") == ()


@pytest.mark.parametrize(
    "cause",
    ["ProductLimitExceeded", "InvalidCurrency", "IncompleteCatalog"],
)
def test_hard_catalog_failure_raises_without_pausing(
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

        def fail(
            url: str,
            *,
            retry_policy: FetchRetryPolicy,
            is_shutdown_requested: Callable[[], bool],
        ) -> tuple[SetecPriceEntry, ...]:
            del url, retry_policy, is_shutdown_requested
            raise FeedFetchError("Setec", cause)

        monkeypatch.setattr(catalog, "fetch_price_index", fail)
        with pytest.raises(FeedFetchError, match=cause):
            monitor.scan()
        assert not sender.messages
