from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.setec_models import SetecPriceEntry, SetecProduct
from tests.setec_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
    make_feed,
    make_monitor,
    make_product,
    snapshots_by_product,
)


def variant_product(amount: int, variant_ids: tuple[str | None, ...]) -> SetecProduct:
    payload = make_product("prod-1", calculated_amount=amount).model_dump()
    variant = payload["variants"][0]
    payload["variants"] = [{**variant, "id": variant_id} for variant_id in variant_ids]
    return SetecProduct.model_validate(payload)


@dataclass
class ConfirmationCatalog:
    indexed: SetecProduct
    displayed: SetecProduct

    def fetch_price_index(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecPriceEntry, ...]:
        del url, retry_policy, is_shutdown_requested
        return (SetecPriceEntry.model_validate(self.indexed.model_dump()),)

    def fetch_products_by_ids(
        self,
        url: str,
        product_ids: Sequence[str],
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecProduct, ...]:
        del url, product_ids, retry_policy, is_shutdown_requested
        return (self.displayed,)


@pytest.mark.parametrize(
    ("indexed_ids", "displayed_ids"),
    [
        (("variant-a",), ("variant-b",)),
        (("variant-a",), (None,)),
        ((None,), ("variant-a",)),
        (("variant-a",), ("variant-a", "variant-b")),
        (("variant-a", "variant-b"), ("variant-b", "variant-a")),
    ],
)
def test_unconfirmed_variant_retains_snapshot_then_recovers_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    indexed_ids: tuple[str | None, ...],
    displayed_ids: tuple[str | None, ...],
) -> None:
    baseline = variant_product(100, ("variant-a",))
    catalog = ConfirmationCatalog(baseline, baseline)
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)
        monitor.scan()
        catalog.indexed = variant_product(90, indexed_ids)
        catalog.displayed = variant_product(90, displayed_ids)
        monitor.scan()
        assert sender.messages == []
        assert snapshots_by_product(store)["prod-1"].amount == Decimal(100)
        assert "Deferred 1" in caplog.text
        assert "hidden" not in caplog.text
        assert "https://" not in caplog.text

        catalog.indexed = catalog.displayed = variant_product(90, ("variant-a",))
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == 1
        assert snapshots_by_product(store)["prod-1"].amount == Decimal(90)


def test_price_disagreement_retains_snapshot_until_a_confirmed_observation(
    tmp_path: Path,
) -> None:
    baseline = variant_product(100, ("variant-a",))
    catalog = ConfirmationCatalog(baseline, baseline)
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)
        monitor.scan()
        catalog.indexed = variant_product(90, ("variant-a",))
        catalog.displayed = variant_product(95, ("variant-a",))
        monitor.scan()
        assert sender.messages == []
        assert snapshots_by_product(store)["prod-1"].amount == Decimal(100)

        catalog.indexed = catalog.displayed
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == 1
        assert snapshots_by_product(store)["prod-1"].amount == Decimal(95)


def test_ambiguous_variants_do_not_establish_a_baseline(tmp_path: Path) -> None:
    product = variant_product(100, ("variant-a", "variant-b"))
    catalog = ConfirmationCatalog(product, product)
    sender = RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        make_monitor(make_feed(), catalog, store, sender).scan()
        assert snapshots_by_product(store) == {}
        assert sender.messages == []


def test_same_title_distinct_products_each_deliver_once(tmp_path: Path) -> None:
    before = tuple(
        make_product(product_id, calculated_amount=100).model_copy(
            update={"title": "Shared product title"},
        )
        for product_id in ("prod-1", "prod-2")
    )
    after = tuple(
        make_product(product_id, calculated_amount=90).model_copy(
            update={"title": "Shared product title"},
        )
        for product_id in ("prod-1", "prod-2")
    )
    catalog = CatalogStub([before, after, after])
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED] * 2)
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == 2
        assert len({message.entry.link for message in sender.messages}) == 2
        assert {s.amount for s in snapshots_by_product(store).values()} == {Decimal(90)}
