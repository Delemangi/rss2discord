from decimal import Decimal
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import SourceMetric
from tests.anhoch_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
    make_feed,
    make_monitor,
    make_product,
    snapshots_by_product,
)


def test_scan_seeds_first_and_later_unseen_products_silently(tmp_path: Path) -> None:
    # Given
    first_product = make_product(1, amount="100", formatted="100 den")
    later_product = make_product(2, amount="200", formatted="200 den")
    catalog = CatalogStub([(first_product,), (first_product, later_product)])
    sender = RecordingSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert sender.messages == []
        assert set(snapshots_by_product(store)) == {1, 2}


def test_scan_ignores_numerically_equal_canonical_selling_price(tmp_path: Path) -> None:
    # Given
    baseline = make_product(1, amount="1.20", formatted="1.20 den")
    equivalent = make_product(1, amount="1.2", formatted="1.20 den")
    sender = RecordingSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(baseline,), (equivalent,)]),
            store,
            sender,
        )

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert sender.messages == []
        assert snapshots_by_product(store)[1].amount == Decimal("1.2")


def test_initial_scan_persists_canonical_high_scale_nonzero_price(
    tmp_path: Path,
) -> None:
    # Given
    product = make_product(
        1,
        amount="1.00000000000000000",
        formatted="1 ден.",
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(product,)]),
            store,
            RecordingSender([]),
        )

        # When
        monitor.scan()

        # Then
        persisted_amount = snapshots_by_product(store)[1].amount
        assert persisted_amount == 1
        assert persisted_amount.as_tuple().exponent == 0


def test_initial_scan_persists_max_sqlite_signed_product_id(tmp_path: Path) -> None:
    # Given
    maximum_product_id = 2**63 - 1
    product = make_product(maximum_product_id, amount="100", formatted="100 ден.")

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(product,)]),
            store,
            RecordingSender([]),
        )

        # When
        monitor.scan()

        # Then
        assert (
            snapshots_by_product(store)[maximum_product_id].product_id
            == maximum_product_id
        )


def test_scan_refreshes_formatting_without_an_alert(tmp_path: Path) -> None:
    # Given
    baseline = make_product(1, amount="100", formatted="100 den")
    reformatted = make_product(1, amount="100", formatted="100.00 ден.")
    sender = RecordingSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(baseline,), (reformatted,)]),
            store,
            sender,
        )

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert sender.messages == []
        assert snapshots_by_product(store)[1].formatted == "100.00 ден."


def test_scan_renders_price_changes_in_catalog_api_order(tmp_path: Path) -> None:
    # Given
    decrease_before = make_product(30, amount="100", formatted="100 den")
    increase_before = make_product(10, amount="100", formatted="100 den")
    currency_before = make_product(20, amount="100", formatted="100 den")
    decrease_after = make_product(30, amount="90", formatted="90 den")
    increase_after = make_product(10, amount="110", formatted="110 den")
    currency_after = make_product(20, amount="100", formatted="$100", currency="USD")
    sender = RecordingSender([True, True, True])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub(
                [
                    (decrease_before, increase_before, currency_before),
                    (decrease_after, increase_after, currency_after),
                ],
            ),
            store,
            sender,
        )

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert [message.entry.title for message in sender.messages] == [
            "Product 30",
            "Product 10",
            "Product 20",
        ]
        assert [message.entry.description for message in sender.messages] == [
            "Price decreased from 100 den to 90 den",
            "Price increased from 100 den to 110 den",
            "Price changed from 100 den to $100",
        ]
        assert sender.messages[0].entry.source_metrics == (
            SourceMetric(label="Price", value="90 den"),
            SourceMetric(label="Previous", value="100 den"),
            SourceMetric(label="Original", value="150 den"),
            SourceMetric(label="Stock", value="3"),
            SourceMetric(label="Installments", value="12 × 10 den"),
        )


def test_failed_send_retries_and_successful_snapshot_survives_reopen(
    tmp_path: Path,
) -> None:
    # Given
    database_path = tmp_path / "state.db"
    baseline = make_product(1, amount="100", formatted="100 den")
    changed = make_product(1, amount="90", formatted="90 den")
    sender = RecordingSender([False, True])

    with DeliveryStore(database_path) as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(baseline,), (changed,), (changed,)]),
            store,
            sender,
        )

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert snapshots_by_product(store)[1].formatted == "100 den"

        # When
        monitor.scan()

        # Then
        assert snapshots_by_product(store)[1].formatted == "90 den"

    with DeliveryStore(database_path) as reopened_store:
        sender_after_reopen = RecordingSender([])
        reopened_monitor = make_monitor(
            make_feed(),
            CatalogStub([(changed,)]),
            reopened_store,
            sender_after_reopen,
        )

        # When
        reopened_monitor.scan()

        # Then
        assert sender_after_reopen.messages == []


def test_failed_product_does_not_suppress_later_changes_or_remove_missing_history(
    tmp_path: Path,
) -> None:
    # Given
    failed_before = make_product(1, amount="100", formatted="100 den")
    missing_before = make_product(2, amount="200", formatted="200 den")
    later_before = make_product(3, amount="300", formatted="300 den")
    failed_after = make_product(1, amount="90", formatted="90 den")
    later_after = make_product(3, amount="290", formatted="290 den")
    sender = RecordingSender([False, True])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub(
                [
                    (failed_before, missing_before, later_before),
                    (failed_after, later_after),
                ],
            ),
            store,
            sender,
        )

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert [message.entry.title for message in sender.messages] == [
            "Product 1",
            "Product 3",
        ]
        snapshots = snapshots_by_product(store)
        assert snapshots[1].formatted == "100 den"
        assert snapshots[2].formatted == "200 den"
        assert snapshots[3].formatted == "290 den"


def test_scan_delays_only_between_accepted_alerts(tmp_path: Path) -> None:
    # Given
    before = tuple(
        make_product(
            product_id,
            amount=str(product_id * 100),
            formatted=f"{product_id}00 den",
        )
        for product_id in (1, 2, 3)
    )
    after = tuple(
        make_product(
            product_id,
            amount=str(product_id * 100 - 1),
            formatted=f"{product_id}99 den",
        )
        for product_id in (1, 2, 3)
    )
    sender = RecordingSender([True, False, True])
    delays: list[float] = []

    def record_delay(seconds: float) -> bool:
        delays.append(seconds)
        return True

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([before, after]),
            store,
            sender,
            sleep=record_delay,
            delay_between_posts=2.5,
        )

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert [message.entry.title for message in sender.messages] == [
            "Product 1",
            "Product 2",
            "Product 3",
        ]
        assert delays == [2.5]
