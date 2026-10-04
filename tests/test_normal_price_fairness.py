"""Normal confirmation selection progresses even when early details never agree."""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.transports.cccenter_models import CCCenterListing, CCCenterProduct
from tests import setec_price_monitor_helpers as setec
from tests.test_adapter_c2_recovery import DetailCatalog, cc_monitor
from tests.test_cccenter_price_monitor import product


def test_setec_deferred_first_ten_cannot_starve_eleventh_after_restart(
    tmp_path: Path,
) -> None:
    ids = tuple(f"prod-{index:02}" for index in range(11))
    baseline = tuple(setec.make_product(pid, calculated_amount=100) for pid in ids)
    changed = tuple(setec.make_product(pid, calculated_amount=90) for pid in ids)
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    path = tmp_path / "state.db"
    first = setec.CatalogStub([baseline, changed], hidden_ids=frozenset(ids[:10]))
    with DeliveryStore(path) as store:
        monitor = setec.make_monitor(setec.make_feed(), first, store, sender)
        monitor.scan()
        monitor.scan()
        assert first.requested_id_batches == [ids[:10]]
        assert not sender.messages
    # Recreate both the store and monitor: progress must not live in memory.
    second = setec.CatalogStub([changed, changed], hidden_ids=frozenset(ids[:10]))
    with DeliveryStore(path) as store:
        monitor = setec.make_monitor(setec.make_feed(), second, store, sender)
        monitor.scan()
        monitor.scan()
        assert second.requested_id_batches[0][0] == ids[-1]
        assert all(len(batch) <= 10 for batch in second.requested_id_batches)
        assert len(sender.messages) == 1
        snapshots = setec.snapshots_by_product(store)
        assert snapshots[ids[-1]].amount == 90
        assert all(snapshots[pid].amount == 100 for pid in ids[:10])


@pytest.mark.parametrize("changed_count", [0, 1])
@pytest.mark.parametrize("winner", ["normal", "approval"])
def test_normal_plan_fences_stale_approval_before_silent_updates_and_sends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_count: int,
    winner: str,
) -> None:
    baseline = tuple(
        setec.make_product(f"p-{i:03}", calculated_amount=100) for i in range(101)
    )
    targets = tuple(setec.make_product(p.id, calculated_amount=90) for p in baseline)
    live = (
        *targets[:changed_count],
        *baseline[changed_count:],
        setec.make_product("fresh", calculated_amount=70),
    )
    catalog = setec.CatalogStub([baseline, targets, live])
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store, DeliveryStore(path) as admin:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()
        candidate = store.list_price_change_batches("setec")[0]

        def approve() -> None:
            admin.approve_price_change_batch(
                feed_id="setec",
                fingerprint=candidate.fingerprint,
                reason="stale approval",
            )

        if winner == "approval":
            select = store.select_normal_price_deliveries

            def approve_then_select(
                *,
                feed_id: str,
                product_ids: Iterable[str],
                limit: int = 10,
            ) -> tuple[str, ...] | None:
                approve()
                return select(feed_id=feed_id, product_ids=product_ids, limit=limit)

            monkeypatch.setattr(
                store,
                "select_normal_price_deliveries",
                approve_then_select,
            )
        else:
            upsert = store.upsert_price_snapshots

            def try_stale_approval_before_silent_write(
                snapshots: Iterable[PriceSnapshot],
            ) -> None:
                with pytest.raises(ValueError, match="not pending approval"):
                    approve()
                upsert(snapshots)

            monkeypatch.setattr(
                store,
                "upsert_price_snapshots",
                try_stale_approval_before_silent_write,
            )
        monitor.scan()
        persisted = setec.snapshots_by_product(store)
        batch = store.load_price_batch(candidate.batch_id)
        assert batch is not None
        if winner == "approval":
            assert batch.status == "approved"
            assert "fresh" not in persisted
            assert not sender.messages
            assert all(s.amount == 100 for s in persisted.values())
        else:
            assert batch.status == "revoked"
            assert persisted["fresh"].amount == 70
            assert len(sender.messages) == changed_count


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


def test_cccenter_deferred_first_ten_cannot_starve_eleventh_after_restart(
    tmp_path: Path,
) -> None:
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
        assert first.calls == [ids[:10]]
        assert not sender.messages
    second = MismatchedCCCatalog([changed, changed])
    with DeliveryStore(path) as store:
        monitor = cc_monitor(store, second, sender)
        monitor.scan()
        monitor.scan()
        assert second.calls[0][0] == ids[-1]
        assert all(len(batch) <= 10 for batch in second.calls)
        assert len(sender.messages) == 1
        snapshots = {s.product_id: s for s in store.load_price_snapshots("cccenter")}
        assert snapshots[ids[-1]].amount == 90
        assert all(snapshots[pid].amount == 100 for pid in ids[:10])
