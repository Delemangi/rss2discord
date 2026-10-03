from dataclasses import replace
from pathlib import Path
from typing import Protocol

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.transports.technomarket_price_monitor import TechnomarketPriceMonitor
from tests import anhoch_price_monitor_helpers as anhoch
from tests import neksio_price_monitor_helpers as neksio
from tests import setec_price_monitor_helpers as setec
from tests import test_gjirafa50_price_monitor as gjirafa
from tests import test_neptun_price_monitor as neptun
from tests import test_pazar3_price_monitor as pazar3
from tests import test_reklama5_price_monitor as reklama5
from tests import test_technomarket_price_monitor as techno
from tests.test_adapter_c2_recovery import DetailCatalog, cc_monitor
from tests.test_cccenter_price_monitor import product


class Monitor(Protocol):
    def scan(self) -> None: ...


def build(
    provider: str,
    count: int,
    store: DeliveryStore,
    sender: setec.RecordingSender,
) -> Monitor:
    if provider == "anhoch":
        before = tuple(
            anhoch.make_product(i + 1, amount="100", formatted="100 MKD")
            for i in range(count)
        )
        after = tuple(
            anhoch.make_product(i + 1, amount="90", formatted="90 MKD")
            for i in range(count)
        )
        return anhoch.make_monitor(
            anhoch.make_feed(),
            anhoch.CatalogStub([before, after, after, after]),
            store,
            sender,
        )
    if provider == "neksio":
        nb = tuple(
            neksio.make_product(i + 1, amount="100", formatted="100 MKD")
            for i in range(count)
        )
        na = tuple(
            neksio.make_product(i + 1, amount="90", formatted="90 MKD")
            for i in range(count)
        )
        return neksio.make_monitor(
            neksio.make_feed(),
            neksio.CatalogStub([nb, na, na, na]),
            store,
            sender,
        )
    if provider == "setec":
        sb = tuple(
            setec.make_product(str(i), calculated_amount=100) for i in range(count)
        )
        sa = tuple(
            setec.make_product(str(i), calculated_amount=90) for i in range(count)
        )
        return setec.make_monitor(
            setec.make_feed(),
            setec.CatalogStub([sb, sa, sa, sa]),
            store,
            sender,
        )
    if provider == "cccenter":
        cb = tuple(
            product("100", f"https://cccenter.mk/product/{i}/") for i in range(count)
        )
        ca = tuple(
            product("90", f"https://cccenter.mk/product/{i}/") for i in range(count)
        )
        return cc_monitor(store, DetailCatalog([cb, ca, ca, ca]), sender)
    if provider == "gjirafa50":
        gb = tuple(gjirafa.make_product(i, 100) for i in range(count))
        ga = tuple(gjirafa.make_product(i, 90) for i in range(count))
        return gjirafa.make_monitor(
            gjirafa.CatalogStub([gb, ga, ga, ga]),
            store,
            sender,
        )
    if provider == "neptun":
        pb = tuple(neptun.make_product(i + 1, 100) for i in range(count))
        pa = tuple(neptun.make_product(i + 1, 90) for i in range(count))
        return neptun.make_monitor(neptun.CatalogStub([pb, pa, pa, pa]), store, sender)
    if provider == "pazar3":
        zb = tuple(pazar3.priced(str(i), "100 MKD") for i in range(count))
        za = tuple(pazar3.priced(str(i), "90 MKD") for i in range(count))
        return pazar3.monitor(pazar3.CatalogStub([zb, za, za, za]), store, sender)
    if provider == "reklama5":
        rb = tuple(reklama5._listing(str(i), "100 ден.") for i in range(count))
        ra = tuple(reklama5._listing(str(i), "90 ден.") for i in range(count))
        return reklama5._monitor(reklama5.CatalogStub([rb, ra, ra, ra]), store, sender)
    tb = tuple(techno.product("100", product_id=str(i)) for i in range(count))
    ta = tuple(techno.product("90", product_id=str(i)) for i in range(count))
    return TechnomarketPriceMonitor(
        FeedConfig(
            id="technomarket",
            url="https://tehnomarket.com.mk/category/4003/laptopi",
            webhook="https://discord.example.test/webhook",
            strategy="technomarket",
        ),
        replace(
            techno.dependencies(techno.CatalogStub([tb, ta, ta, ta]), sender),
            snapshots=store,
        ),
    )


@pytest.mark.parametrize(
    "provider",
    [
        "anhoch",
        "neksio",
        "setec",
        "cccenter",
        "gjirafa50",
        "neptun",
        "pazar3",
        "reklama5",
        "technomarket",
    ],
)
@pytest.mark.parametrize("count", [100, 101])
@pytest.mark.parametrize(
    "outcome",
    [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.DELIVERED],
)
def test_every_adapter_caps_normal_and_approved_attempts(
    tmp_path: Path,
    provider: str,
    count: int,
    outcome: DiscordDeliveryResult,
) -> None:
    sender = setec.RecordingSender([outcome] * 20)
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = build(provider, count, store, sender)
        monitor.scan()
        monitor.scan()
        if count == 101:
            assert sender.messages == []
            candidate = store.list_price_change_batches(feed_id=provider)[0]
            store.approve_price_change_batch(
                feed_id=provider,
                fingerprint=candidate.fingerprint,
                reason="fixture review",
            )
            monitor.scan()
        assert len(sender.messages) == 10
        first = {message.entry.link for message in sender.messages}
        monitor.scan()
        assert len(sender.messages) == 20
        if count == 101:
            assert first.isdisjoint(
                message.entry.link for message in sender.messages[10:]
            )
            batch = store.load_active_price_batch(provider)
            assert batch is not None
            assert sum(item.attempt_count for item in batch.items) == 20
            assert batch.pending_count == (
                81 if outcome is DiscordDeliveryResult.DELIVERED else 101
            )
        snapshots = store.load_price_snapshots(provider)
        assert sum(snapshot.amount == 90 for snapshot in snapshots) == (
            20 if outcome is DiscordDeliveryResult.DELIVERED else 0
        )
