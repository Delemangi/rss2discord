"""Small scans send every confirmable change without legacy rotation starvation."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import PriceChangeRecord
from rss2discord.providers.cccenter.models import CCCenterListing, CCCenterProduct
from tests import setec_price_monitor_helpers as setec
from tests.test_adapter_c2_recovery import DetailCatalog, cc_monitor
from tests.test_cccenter_price_monitor import product


def test_setec_small_scan_never_starves_hidden_detail_ids(tmp_path: Path) -> None:
    ids = tuple(f"prod-{index:02}" for index in range(11))
    baseline = tuple(setec.make_product(pid, calculated_amount=100) for pid in ids)
    changed = tuple(setec.make_product(pid, calculated_amount=90) for pid in ids)
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    path = tmp_path / "state.db"
    first = setec.CatalogStub(
        [baseline, changed],
        hidden_ids=frozenset(ids[:10]),
    )
    with DeliveryStore(path) as store:
        monitor = setec.make_monitor(setec.make_feed(), first, store, sender)
        monitor.scan()
        monitor.scan()
        assert len(first.requested_id_batches) == 1
        assert first.requested_id_batches[0] == ids
        assert len(sender.messages) == 1
        snapshots = setec.snapshots_by_product(store)
        assert snapshots[ids[-1]].amount == 90
        assert all(snapshots[pid].amount == 100 for pid in ids[:10])
    second = setec.CatalogStub(
        [changed, changed],
        hidden_ids=frozenset(ids[:10]),
    )
    with DeliveryStore(path) as store:
        monitor = setec.make_monitor(setec.make_feed(), second, store, sender)
        monitor.scan()
        monitor.scan()
        assert second.requested_id_batches[0] == ids[:10]
        assert len(sender.messages) == 1
        assert store.list_health("setec")[0].cause == "PriceConfirmationDeferred"


class MismatchedCCCatalog(DetailCatalog):
    def fetch_product_details(
        self,
        listings: Sequence[CCCenterListing | CCCenterProduct],
        *,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[CCCenterProduct, ...]:
        details = super().fetch_product_details(
            listings,
            is_shutdown_requested=is_shutdown_requested,
        )
        return tuple(
            item
            if item.product_id.endswith("item-10/")
            else replace(item, current_price=Decimal(100))
            for item in details
        )


def test_cccenter_small_scan_confirms_every_selected_id(tmp_path: Path) -> None:
    baseline = tuple(
        product("100", f"https://cccenter.mk/product/item-{i:02}/") for i in range(11)
    )
    changed = tuple(replace(item, current_price=Decimal(90)) for item in baseline)
    ids = tuple(item.product_id for item in baseline)
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    path = tmp_path / "state.db"
    first = MismatchedCCCatalog([baseline, changed])
    with DeliveryStore(path) as store:
        monitor = cc_monitor(store, first, sender)
        monitor.scan()
        monitor.scan()
        assert all(len(batch) <= 10 for batch in first.calls)
        assert [len(batch) for batch in first.calls] == [10, 1]
        assert len(sender.messages) == 1
        snapshots = {s.product_id: s for s in store.load_price_snapshots("cccenter")}
        assert snapshots[ids[-1]].amount == 90
        assert all(snapshots[pid].amount == 100 for pid in ids[:10])


def test_legacy_approval_or_pause_does_not_fence_small_scan(tmp_path: Path) -> None:
    ids = tuple(f"p-{index:03}" for index in range(25))
    items = tuple(
        PriceChangeRecord(
            pid,
            setec.PriceSnapshot("setec", pid, Decimal(100), "100", "MKD"),
            setec.PriceSnapshot("setec", pid, Decimal(90), "90", "MKD"),
        )
        for pid in ids
    )
    baseline = tuple(setec.make_product(pid, calculated_amount=100) for pid in ids)
    changed = tuple(setec.make_product(pid, calculated_amount=90) for pid in ids)
    fresh = setec.make_product("fresh", calculated_amount=70)
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED] * 25)
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        store.upsert_price_snapshots(item.previous for item in items)
        monitor = setec.make_monitor(
            setec.make_feed(),
            setec.CatalogStub([(*baseline, fresh), (*changed, fresh)]),
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == 25
        assert store.count_pending_price_notifications("setec") == 0
        persisted = setec.snapshots_by_product(store)
        assert persisted["fresh"].amount == 70
