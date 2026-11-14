from pathlib import Path
from typing import cast

from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.providers.setec.models import SetecProduct
from tests import setec_price_monitor_helpers as setec


def product_with_variant_id(
    product: SetecProduct,
    variant_id: str | None,
) -> SetecProduct:
    payload = product.model_dump()
    variants = cast(list[dict[str, object]], payload["variants"])
    payload["variants"] = [{**variant, "id": variant_id} for variant in variants]
    return SetecProduct.model_validate(payload)


def test_both_missing_variant_ids_defer_without_snapshot_mutation_then_recover(
    tmp_path: Path,
) -> None:
    baseline = setec.make_product("prod-1", calculated_amount=100)
    missing = product_with_variant_id(
        setec.make_product("prod-1", calculated_amount=90),
        None,
    )
    matching = setec.make_product("prod-1", calculated_amount=90)
    catalog = setec.CatalogStub([(baseline,), (missing,), (matching,)])
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()

        monitor.scan()
        assert sender.messages == []
        assert setec.snapshots_by_product(store)["prod-1"].amount == 100
        health = store.list_health("setec")[0]
        assert (health.state, health.cause) == (
            "failed",
            "PriceConfirmationDeferred",
        )

        monitor.scan()
        assert len(sender.messages) == 1
        assert setec.snapshots_by_product(store)["prod-1"].amount == 90


def test_approved_batch_missing_variant_id_is_excluded_from_digest(
    tmp_path: Path,
) -> None:
    baseline = tuple(
        setec.make_product(str(index), calculated_amount=100) for index in range(101)
    )
    changed = tuple(
        setec.make_product(str(index), calculated_amount=90) for index in range(101)
    )
    missing = product_with_variant_id(changed[0], None)
    catalog = setec.CatalogStub(
        [baseline, changed, changed],
        display_overrides={changed[0].id: missing},
    )
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = setec.make_monitor(setec.make_feed(), catalog, store, sender)
        monitor.scan()
        monitor.scan()

        assert len(sender.messages) == 1
        assert len(sender.messages[0].files) == 2
        assert store.count_pending_price_notifications("setec") == 0
        snapshots = store.load_price_snapshots("setec")
        assert sum(snapshot.amount == 90 for snapshot in snapshots) == 100
        assert sum(snapshot.amount == 100 for snapshot in snapshots) == 1
