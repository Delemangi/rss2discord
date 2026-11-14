from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import (
    DiscordDeliveryResult,
    SleepCallback,
    WebhookMessage,
)
from rss2discord.fetch_errors import FeedFetchError
from tests.anhoch_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
    make_feed,
    make_monitor,
    make_product,
    snapshots_by_product,
)


def test_formatting_refresh_and_change_are_both_applied(
    tmp_path: Path,
) -> None:
    baseline = make_product(1, amount="100", formatted="100 den")
    changed = make_product(2, amount="90", formatted="90 den")
    sender = RecordingSender([True])
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
        monitor.scan()

        snapshots = snapshots_by_product(store)
        assert snapshots["1"].formatted == "100.00 den"
        assert snapshots["2"].amount == 90
        assert store.list_health("anhoch")[0].item_count == 2
        assert len(sender.messages) == 1
        assert sender.messages[0].entry.title == "Product 2"


@pytest.mark.parametrize("invalid", [None, "missing", "currency", "formatting"])
def test_changed_diff_sends_or_raises_without_legacy_approval(
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
    sender = RecordingSender([True])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(baseline, unchanged), current, current]),
            store,
            sender,
        )
        monitor.scan()
        previous = snapshots_by_product(store)["1"]
        try:
            monitor.scan()
        except FeedFetchError:
            assert invalid == "currency"
            assert snapshots_by_product(store)["1"] == previous
            assert sender.messages == []
            return

        snapshots = snapshots_by_product(store)
        if invalid in {None, "formatting"}:
            assert snapshots["1"].amount == 90
            assert snapshots["3"].amount == 70
            assert len(sender.messages) == 1
            monitor.scan()
            assert snapshots_by_product(store)["2"].formatted == "100.00 den"
            assert snapshots_by_product(store)["3"].amount == 70
        elif invalid == "missing":
            assert snapshots["1"] == previous
            assert snapshots["3"].amount == 70
            assert sender.messages == []
        else:
            assert snapshots["1"] == previous
            assert snapshots["2"].formatted == "100.00 den"
            assert snapshots["3"].amount == 70
            assert sender.messages == []


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
