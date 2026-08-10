from decimal import Decimal
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import SourceMetric
from tests.setec_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
    make_feed,
    make_monitor,
    make_product,
    snapshots_by_product,
)


def test_scan_seeds_first_and_later_unseen_products_silently(tmp_path: Path) -> None:
    # Given
    first_product = make_product("prod-1", calculated_amount=100)
    later_product = make_product("prod-2", calculated_amount=200)
    catalog = CatalogStub([(first_product,), (first_product, later_product)])
    sender = RecordingSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert sender.messages == []
        assert set(snapshots_by_product(store)) == {"prod-1", "prod-2"}


def test_silent_baseline_scan_fetches_no_display_data(tmp_path: Path) -> None:
    # Given
    products = tuple(
        make_product(f"prod-{product_number}", calculated_amount=product_number * 100)
        for product_number in (1, 2, 3)
    )
    catalog = CatalogStub([products])
    sender = RecordingSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()

        # Then
        assert sender.messages == []
        assert set(snapshots_by_product(store)) == {"prod-1", "prod-2", "prod-3"}
        assert catalog.requested_id_batches == []


def test_initial_scan_persists_exact_fractional_setec_amount_after_reopen(
    tmp_path: Path,
) -> None:
    # Given
    database_path = tmp_path / "state.db"
    amount = Decimal("651261.49217128")
    product = make_product("prod-1", calculated_amount=amount)
    sender = RecordingSender([])

    with DeliveryStore(database_path) as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(product,)]),
            store,
            sender,
        )

        # When
        monitor.scan()

    with DeliveryStore(database_path) as reopened_store:
        snapshot = snapshots_by_product(reopened_store)["prod-1"]

    # Then
    assert sender.messages == []
    assert snapshot.amount == amount


def test_scan_skips_unpriced_product_until_its_first_price_appears(
    tmp_path: Path,
) -> None:
    # Given
    first_price = make_product("prod-1", calculated_amount=100)
    catalog = CatalogStub([(), (), (first_price,)])
    sender = RecordingSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()
        monitor.scan()

        # Then
        assert sender.messages == []
        assert catalog.requested_id_batches == []
        snapshot = snapshots_by_product(store)["prod-1"]
        assert snapshot.amount == Decimal(100)
        assert snapshot.formatted == "100 ден."
        assert snapshot.currency == "MKD"


def test_scan_retains_prior_snapshot_when_previously_priced_product_has_no_variants(
    tmp_path: Path,
) -> None:
    # Given
    prior_snapshot = PriceSnapshot(
        feed_id="setec",
        product_id="prod-1",
        amount=Decimal(100),
        formatted="100 ден.",
        currency="MKD",
    )
    sender = RecordingSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(prior_snapshot)
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(make_product("prod-1", calculated_amount=None),)]),
            store,
            sender,
        )

        # When
        monitor.scan()

        # Then
        assert sender.messages == []
        assert snapshots_by_product(store)["prod-1"] == prior_snapshot


def test_scan_delivers_ordered_price_changes_with_exact_setec_message_fields(
    tmp_path: Path,
) -> None:
    # Given
    decrease_before = make_product("prod-30", calculated_amount=100)
    increase_before = make_product("prod-10", calculated_amount=200)
    decrease_after = make_product(
        "prod-30",
        calculated_amount=90,
        original_amount=120,
    )
    increase_after = make_product("prod-10", calculated_amount=210)
    feed = make_feed()
    sender = RecordingSender(
        [DiscordDeliveryResult.DELIVERED, DiscordDeliveryResult.DELIVERED],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            feed,
            CatalogStub(
                [
                    (decrease_before, increase_before),
                    (decrease_after, increase_after),
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
            "Product prod-30",
            "Product prod-10",
        ]
        assert [message.entry.description for message in sender.messages] == [
            "Price decreased from 100 ден. to 90 ден.",
            "Price increased from 200 ден. to 210 ден.",
        ]
        assert (
            sender.messages[0].entry.link == "https://setec.mk/products/product-prod-30"
        )
        assert sender.messages[0].entry.image_url == (
            "https://images.example.test/prod-30.webp"
        )
        assert sender.messages[0].entry.categories == ("Computers", "Accessories")
        assert sender.messages[0].entry.source_metrics == (
            SourceMetric(label="Price", value="90 ден."),
            SourceMetric(label="Previous", value="100 ден."),
            SourceMetric(label="Original", value="120 ден."),
        )
        assert sender.messages[0].source_title == "Setec Deals"


def test_alert_omits_original_when_display_document_reports_another_price(
    tmp_path: Path,
) -> None:
    # Given
    baseline = make_product("prod-1", calculated_amount=100)
    indexed_change = make_product("prod-1", calculated_amount=90)
    diverged_display = make_product(
        "prod-1",
        calculated_amount=95,
        original_amount=150,
    )
    catalog = CatalogStub(
        [(baseline,), (indexed_change,)],
        display_overrides={"prod-1": diverged_display},
    )
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert catalog.requested_id_batches == [("prod-1",)]
        assert [message.entry.title for message in sender.messages] == [
            "Product prod-1",
        ]
        assert sender.messages[0].entry.source_metrics == (
            SourceMetric(label="Price", value="90 ден."),
            SourceMetric(label="Previous", value="100 ден."),
        )
        assert sender.messages[0].entry.description == (
            "Price decreased from 100 ден. to 90 ден."
        )


def test_alert_carries_original_from_display_document_when_prices_agree(
    tmp_path: Path,
) -> None:
    # Given
    baseline = make_product("prod-1", calculated_amount=100)
    indexed_change = make_product("prod-1", calculated_amount=90)
    discounted_display = make_product(
        "prod-1",
        calculated_amount=90,
        original_amount=150,
    )
    catalog = CatalogStub(
        [(baseline,), (indexed_change,)],
        display_overrides={"prod-1": discounted_display},
    )
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert catalog.requested_id_batches == [("prod-1",)]
        assert [message.entry.title for message in sender.messages] == [
            "Product prod-1",
        ]
        assert sender.messages[0].entry.source_metrics == (
            SourceMetric(label="Price", value="90 ден."),
            SourceMetric(label="Previous", value="100 ден."),
            SourceMetric(label="Original", value="150 ден."),
        )


def test_alert_ships_without_original_when_display_document_has_no_variants(
    tmp_path: Path,
) -> None:
    # Given
    baseline = make_product("prod-1", calculated_amount=100)
    indexed_change = make_product("prod-1", calculated_amount=90)
    unpriced_display = make_product("prod-1", calculated_amount=None)
    catalog = CatalogStub(
        [(baseline,), (indexed_change,)],
        display_overrides={"prod-1": unpriced_display},
    )
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert unpriced_display.variants == ()
        assert [message.entry.title for message in sender.messages] == [
            "Product prod-1",
        ]
        assert sender.messages[0].entry.source_metrics == (
            SourceMetric(label="Price", value="90 ден."),
            SourceMetric(label="Previous", value="100 ден."),
        )
        assert snapshots_by_product(store)["prod-1"].amount == Decimal(90)


def test_scan_requests_display_data_only_for_changed_product_ids(
    tmp_path: Path,
) -> None:
    # Given
    unchanged = make_product("prod-unchanged", calculated_amount=100)
    changed_before = make_product("prod-changed", calculated_amount=200)
    changed_after = make_product("prod-changed", calculated_amount=190)
    appearing = make_product("prod-new", calculated_amount=300)
    dropped = make_product("prod-dropped", calculated_amount=400)
    catalog = CatalogStub(
        [
            (unchanged, changed_before, dropped),
            (unchanged, changed_after, appearing),
        ],
    )
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert catalog.requested_id_batches == [("prod-changed",)]
        assert [message.entry.title for message in sender.messages] == [
            "Product prod-changed",
        ]
        snapshots = snapshots_by_product(store)
        assert snapshots["prod-unchanged"].amount == Decimal(100)
        assert snapshots["prod-changed"].amount == Decimal(190)
        assert snapshots["prod-new"].amount == Decimal(300)
        assert snapshots["prod-dropped"].amount == Decimal(400)


def test_product_missing_from_display_fetch_is_skipped_without_persisting_or_sending(
    tmp_path: Path,
) -> None:
    # Given
    hidden_before = make_product("prod-1", calculated_amount=100)
    visible_before = make_product("prod-2", calculated_amount=200)
    hidden_after = make_product("prod-1", calculated_amount=90)
    visible_after = make_product("prod-2", calculated_amount=190)
    catalog = CatalogStub(
        [
            (hidden_before, visible_before),
            (hidden_after, visible_after),
        ],
        hidden_ids=frozenset({"prod-1"}),
    )
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert catalog.requested_id_batches == [("prod-1", "prod-2")]
        assert [message.entry.title for message in sender.messages] == [
            "Product prod-2",
        ]
        snapshots = snapshots_by_product(store)
        assert snapshots["prod-1"].amount == Decimal(100)
        assert snapshots["prod-2"].amount == Decimal(190)


def test_scan_orders_alerts_by_price_index_not_display_response_order(
    tmp_path: Path,
) -> None:
    # Given
    first_before = make_product("prod-1", calculated_amount=100)
    second_before = make_product("prod-2", calculated_amount=200)
    first_after = make_product("prod-1", calculated_amount=90)
    second_after = make_product("prod-2", calculated_amount=190)
    catalog = CatalogStub(
        [
            (first_before, second_before),
            (first_after, second_after),
        ],
        reverse_display=True,
    )
    sender = RecordingSender(
        [DiscordDeliveryResult.DELIVERED, DiscordDeliveryResult.DELIVERED],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert [message.entry.title for message in sender.messages] == [
            "Product prod-1",
            "Product prod-2",
        ]


def test_display_fetch_result_for_unrequested_id_is_ignored(tmp_path: Path) -> None:
    # Given
    baseline = make_product("prod-1", calculated_amount=100)
    changed = make_product("prod-1", calculated_amount=90)
    catalog = CatalogStub(
        [(baseline,), (changed,)],
        extra_products=(make_product("prod-9", calculated_amount=900),),
    )
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert catalog.requested_id_batches == [("prod-1",)]
        assert [message.entry.title for message in sender.messages] == [
            "Product prod-1",
        ]
        assert set(snapshots_by_product(store)) == {"prod-1"}


def test_scan_repairs_formatting_drift_silently_without_display_fetch(
    tmp_path: Path,
) -> None:
    # Given
    drifted_snapshot = PriceSnapshot(
        feed_id="setec",
        product_id="prod-1",
        amount=Decimal(100),
        formatted="100.00 MKD",
        currency="MKD",
    )
    catalog = CatalogStub([(make_product("prod-1", calculated_amount=100),)])
    sender = RecordingSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(drifted_snapshot)
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()

        # Then
        assert sender.messages == []
        assert catalog.requested_id_batches == []
        snapshot = snapshots_by_product(store)["prod-1"]
        assert snapshot.formatted == "100 ден."
        assert snapshot.amount == Decimal(100)
        assert snapshot.currency == "MKD"


def test_original_price_change_without_calculated_price_change_is_silent(
    tmp_path: Path,
) -> None:
    # Given
    baseline = make_product("prod-1", calculated_amount=100, original_amount=120)
    original_only_change = make_product(
        "prod-1",
        calculated_amount=100,
        original_amount=140,
    )
    catalog = CatalogStub([(baseline,), (original_only_change,)])
    sender = RecordingSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert sender.messages == []
        assert catalog.requested_id_batches == []
        assert snapshots_by_product(store)["prod-1"].amount == Decimal(100)


def test_failed_send_retries_without_losing_missing_product_history(
    tmp_path: Path,
) -> None:
    # Given
    failed_before = make_product("prod-1", calculated_amount=100)
    missing_before = make_product("prod-2", calculated_amount=200)
    later_before = make_product("prod-3", calculated_amount=300)
    failed_after = make_product("prod-1", calculated_amount=90)
    later_after = make_product("prod-3", calculated_amount=290)
    sender = RecordingSender(
        [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.DELIVERED],
    )

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
            "Product prod-1",
            "Product prod-3",
        ]
        snapshots = snapshots_by_product(store)
        assert snapshots["prod-1"].amount == Decimal(100)
        assert snapshots["prod-2"].amount == Decimal(200)
        assert snapshots["prod-3"].amount == Decimal(290)


def test_interrupted_delivery_does_not_advance_its_snapshot(tmp_path: Path) -> None:
    # Given
    baseline = make_product("prod-1", calculated_amount=100)
    changed = make_product("prod-1", calculated_amount=90)
    sender = RecordingSender([DiscordDeliveryResult.INTERRUPTED])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(baseline,), (changed,)]),
            store,
            sender,
        )

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert len(sender.messages) == 1
        assert snapshots_by_product(store)["prod-1"].amount == Decimal(100)


def test_scan_delays_only_between_accepted_alerts(tmp_path: Path) -> None:
    # Given
    before = tuple(
        make_product(f"prod-{product_number}", calculated_amount=product_number * 100)
        for product_number in (1, 2, 3)
    )
    after = tuple(
        make_product(
            f"prod-{product_number}",
            calculated_amount=product_number * 100 - 1,
        )
        for product_number in (1, 2, 3)
    )
    sender = RecordingSender(
        [
            DiscordDeliveryResult.DELIVERED,
            DiscordDeliveryResult.FAILED,
            DiscordDeliveryResult.DELIVERED,
        ],
    )
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
            "Product prod-1",
            "Product prod-2",
            "Product prod-3",
        ]
        assert delays == [2.5]
