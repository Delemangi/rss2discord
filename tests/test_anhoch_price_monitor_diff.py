from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import (
    DiscordDeliveryResult,
    SleepCallback,
    WebhookMessage,
)
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.recovery_models import PriceChangeRecord
from tests.anhoch_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
    make_feed,
    make_monitor,
    make_product,
    snapshots_by_product,
)


def test_held_formatting_refresh_and_unfiltered_health_count_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = make_product(1, amount="100", formatted="100 den")
    changed = make_product(2, amount="90", formatted="90 den")
    sender = RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub(
                [
                    (baseline, make_product(2, amount="100", formatted="100 den")),
                    (
                        make_product(1, amount="100", formatted="100.00 den"),
                        changed,
                    ),
                ],
            ),
            store,
            sender,
        )
        monitor.scan()
        monkeypatch.setattr(
            store,
            "held_price_product_ids",
            lambda _: frozenset({"1", "2"}),
        )
        monitor.scan()

        snapshots = snapshots_by_product(store)
        assert snapshots["1"].formatted == "100.00 den"
        assert snapshots["2"].amount == 100
        assert store.list_health("anhoch")[0].item_count == 2
        assert sender.messages == []


@pytest.mark.parametrize("invalid", [None, "missing", "currency", "formatting"])
def test_approved_diff_defers_silent_updates_and_validates_full_target(
    tmp_path: Path,
    invalid: str | None,
) -> None:
    baseline = make_product(1, amount="100", formatted="100 den")
    target = make_product(1, amount="90", formatted="90 den")
    unchanged = make_product(2, amount="100", formatted="100 den")
    reformatted = make_product(2, amount="100", formatted="100.00 den")
    fresh = make_product(3, amount="70", formatted="70 den")
    live = target
    if invalid == "currency":
        live = make_product(1, amount="90", formatted="90 EUR", currency="EUR")
    elif invalid == "formatting":
        live = make_product(1, amount="90", formatted="90.00 den")
    current = (
        (reformatted, fresh) if invalid == "missing" else (live, reformatted, fresh)
    )
    sender = RecordingSender([True] if invalid is None else [])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(baseline, unchanged), current, current]),
            store,
            sender,
        )
        monitor.scan()
        previous = snapshots_by_product(store)["1"]
        record = PriceChangeRecord(
            "1",
            previous,
            replace(previous, amount=Decimal(90), formatted="90 den"),
        )
        fingerprint = canonical_manifest_fingerprint(
            feed_id="anhoch",
            provider="anhoch",
            items=(record,),
        )
        store.record_price_change_candidate(
            feed_id="anhoch",
            provider="anhoch",
            fingerprint=fingerprint,
            catalog_count=2,
            available_count=2,
            items=(record,),
        )
        store.approve_price_change_batch(
            feed_id="anhoch",
            fingerprint=fingerprint,
            reason="fixture review",
        )

        monitor.scan()

        snapshots = snapshots_by_product(store)
        assert snapshots["2"].formatted == "100 den"
        assert "3" not in snapshots
        if invalid is None:
            assert snapshots["1"].amount == 90
            assert len(sender.messages) == 1
            assert store.load_active_price_batch("anhoch") is None
            monitor.scan()
            assert snapshots_by_product(store)["2"].formatted == "100.00 den"
            assert snapshots_by_product(store)["3"].amount == 70
        else:
            assert snapshots["1"] == previous
            assert sender.messages == []
            active = store.load_active_price_batch("anhoch")
            assert active is not None
            assert active.status == "paused"
            assert active.items[0].attempt_count == 0
            monitor.scan()
            assert snapshots_by_product(store) == snapshots


def test_normal_silent_writes_precede_send_and_changed_snapshot_follows_success(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:

        class ObservingSender(RecordingSender):
            def send(
                self,
                message: WebhookMessage,
                sleep: SleepCallback,
            ) -> DiscordDeliveryResult:
                snapshots = snapshots_by_product(store)
                assert snapshots["1"].amount == 100
                assert snapshots["2"].formatted == "100.00 den"
                assert snapshots["3"].amount == 70
                return super().send(message, sleep)

        sender = ObservingSender([True])
        monitor = make_monitor(
            make_feed(),
            CatalogStub(
                [
                    tuple(
                        make_product(i, amount="100", formatted="100 den")
                        for i in (1, 2)
                    ),
                    (
                        make_product(1, amount="90", formatted="90 den"),
                        make_product(2, amount="100", formatted="100.00 den"),
                        make_product(3, amount="70", formatted="70 den"),
                    ),
                ],
            ),
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == 1
        assert snapshots_by_product(store)["1"].amount == 90


def test_currency_gate_precedes_all_silent_writes(tmp_path: Path) -> None:
    sender = RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub(
                [
                    (make_product(1, amount="100", formatted="100 den"),),
                    (
                        make_product(
                            1,
                            amount="100",
                            formatted="100 EUR",
                            currency="EUR",
                        ),
                        make_product(2, amount="50", formatted="50 den"),
                    ),
                ],
            ),
            store,
            sender,
        )
        monitor.scan()
        before = snapshots_by_product(store)
        with pytest.raises(FeedFetchError, match="CurrencyChanged"):
            monitor.scan()
        assert snapshots_by_product(store) == before
        assert sender.messages == []
