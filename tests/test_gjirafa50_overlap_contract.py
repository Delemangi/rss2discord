"""Public catalog contract with inclusive index filters, not displayed prices."""

from collections import Counter
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from math import ceil
from pathlib import Path
from unittest.mock import Mock

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.providers.gjirafa50 import catalog, http, prices
from rss2discord.providers.gjirafa50.catalog import Gjirafa50CatalogClient
from rss2discord.providers.gjirafa50.http import (
    FetchedGjirafa50Page,
    Gjirafa50HttpClient,
    Gjirafa50PageRequest,
)
from rss2discord.providers.gjirafa50.models import (
    Gjirafa50CatalogPage,
    Gjirafa50Product,
)
from rss2discord.providers.gjirafa50.prices import (
    Gjirafa50PriceMonitor,
    Gjirafa50PriceMonitorDependencies,
)
from rss2discord.retries import SQLiteRetryPolicy
from rss2discord.transports import FeedFetchError
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.gjirafa50_helpers import ROOT_URL, RecordingGet, no_wait_retry_policy
from tests.setec_price_monitor_helpers import RecordingSender


class OverlappingStore:
    """Finite known root members; leaf counts describe physical cards.

    Every returned ID belongs to this fake's root. Exact cardinality is a
    completeness argument under that assumption, not a membership/security proof.
    Index prices and displayed prices are intentionally unrelated.
    """

    def __init__(self, total: int = 5, defect: str = "") -> None:
        self.total = total
        self.defect = defect
        self.maximum = total - 1
        self.calls: list[tuple[tuple[int, int] | None, int]] = []
        self.visits: Counter[tuple[tuple[int, int] | None, int]] = Counter()
        self.attempt: datetime | None = None
        self.lower_cards: list[int] | None = None
        self.upper_cards: list[int] | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch, cap: int = 3) -> None:
        monkeypatch.setattr(catalog, "MAX_GJIRAFA50_SHARD_PRODUCTS", cap)
        monkeypatch.setattr(
            catalog,
            "MAX_GJIRAFA50_PRICE_EXCLUSIVE_CENTS",
            self.maximum + 1,
        )
        monkeypatch.setattr(http, "_create_session", lambda: RecordingGet([]))

        def fetch_page(
            _client: Gjirafa50HttpClient,
            root_url: str,
            request: Gjirafa50PageRequest,
            observed_at: datetime,
        ) -> FetchedGjirafa50Page:
            assert root_url == ROOT_URL
            return self.fetch(request, observed_at)

        monkeypatch.setattr(Gjirafa50HttpClient, "fetch_page", fetch_page)

    def fetch(
        self,
        request: Gjirafa50PageRequest,
        observed_at: datetime,
    ) -> FetchedGjirafa50Page:
        request.budget.before_request()
        request.budget.consume_bytes(1_024)
        # Fast retries can have equal timestamps on Windows; each attempt owns
        # a distinct timestamp object, shared by all its requests.
        if self.attempt is not observed_at:
            self.attempt = observed_at
            self.visits.clear()
        bounds = None
        if request.price_range is not None:
            bounds = (
                request.price_range.minimum_cents,
                request.price_range.maximum_exclusive_cents - 1,
            )
        key = (bounds, request.page)
        self.calls.append(key)
        self.visits[key] += 1
        visit = self.visits[key]
        whole = bounds is None or bounds == (0, self.maximum)
        ids = list(range(1, self.total + 1))
        if not whole:
            assert bounds is not None
            ids = [i for i in ids if bounds[0] <= min(i - 1, self.maximum) <= bounds[1]]
            if bounds[0] == 0 and self.lower_cards is not None:
                ids = self.lower_cards
            elif bounds[1] == self.maximum and self.upper_cards is not None:
                ids = self.upper_cards
        total = self.total if whole else len(ids)
        if self.defect == "excess" and whole:
            total -= 1
        if self.defect == "root_changed" and bounds is None and visit == 2:
            total += 1
        lower = bounds == (0, self.maximum // 2)
        if lower and self.defect == "leaf_changed" and visit >= 3:
            total += 1
        start = (request.page - 1) * 24
        products = tuple(
            Gjirafa50Product(
                id=i,
                title=f"Product {i}",
                link=f"{ROOT_URL}product-{i}",
                image_url=None,
                price=Decimal(128),
                currency="MKD",
                formatted_price="128,00 MKD.",
                observed_at=observed_at,
            )
            for i in ids[start : start + 24]
        )
        pages = ceil(total / 24)
        if lower and visit == 2 and request.page == 1:
            if self.defect == "missing":
                products = (products[0], products[0], *products[2:])
            elif self.defect == "short_page":
                products = products[:-1]
            elif self.defect == "wrong_pages":
                pages += 1
            elif self.defect == "page_count_changed":
                total += 1
        upper = bounds == (self.maximum // 2, self.maximum)
        if upper and visit == 2 and request.page == 1 and products:
            first = products[0]
            if self.defect == "price":
                first = replace(first, price=Decimal(129))
            elif self.defect == "currency":
                first = replace(first, currency="EUR")
            elif self.defect == "metadata":
                first = replace(first, title="Changed title")
            products = (first, *products[1:])
        if self.defect == "late_conflict" and upper and request.page == 2 and products:
            products = (replace(products[0], image_url=f"{ROOT_URL}changed.jpg"),)
        if self.defect == "nonempty_terminal" and lower and request.page == pages + 1:
            products = (
                Gjirafa50Product(
                    1,
                    "Product 1",
                    f"{ROOT_URL}product-1",
                    None,
                    Decimal(128),
                    "MKD",
                    "128,00 MKD.",
                    observed_at,
                ),
            )
        return FetchedGjirafa50Page(
            Gjirafa50CatalogPage(total, pages, products),
            1_024,
        )


def fetch_catalog() -> tuple[Gjirafa50Product, ...]:
    return Gjirafa50CatalogClient().fetch_catalog(
        ROOT_URL,
        retry_policy=no_wait_retry_policy(),
        is_shutdown_requested=lambda: False,
    )


def test_overlapping_shards_accept_identical_outside_display_prices_only_after_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OverlappingStore()
    store.install(monkeypatch)

    products = fetch_catalog()

    assert {p.id for p in products} == set(range(1, 6))
    assert len(products) == 5
    assert all(p.price == Decimal(128) for p in products)
    # Wire [0,4] splits to [0,2] and [2,4], so ID 3 occurs in both.
    # Each leaf is probed, enumerated, terminal-checked, then re-counted.
    assert Counter(store.calls) == Counter(
        {
            (None, 1): 2,
            ((0, 4), 1): 1,
            ((0, 2), 1): 3,
            ((0, 2), 2): 1,
            ((2, 4), 1): 3,
            ((2, 4), 2): 1,
        },
    )
    assert store.calls[-3:] == [(None, 1), ((0, 2), 1), ((2, 4), 1)]


@pytest.mark.parametrize("duplicate_position", [1, 24])
def test_identical_cards_within_or_across_native_pages_do_not_invent_coverage(
    monkeypatch: pytest.MonkeyPatch,
    duplicate_position: int,
) -> None:
    store = OverlappingStore(49)
    store.lower_cards = list(range(1, 26))
    store.lower_cards.insert(duplicate_position, 1)
    store.install(monkeypatch, cap=26)

    products = fetch_catalog()

    assert len(products) == len({p.id for p in products}) == 49
    assert {p.id for p in products} == set(range(1, 50))
    assert ((0, 24), 2) in store.calls
    assert ((0, 24), 3) in store.calls  # Native terminal page, not early success.


@pytest.mark.parametrize("field", ["price", "currency", "metadata"])
def test_inconsistent_repeated_product_fails_retryably(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    store = OverlappingStore(defect=field)
    store.install(monkeypatch)

    with pytest.raises(FeedFetchError) as error:
        fetch_catalog()

    assert error.value.retryable is True
    assert sum(bounds is None for bounds, _ in store.calls) == 3
    assert ((2, 4), 1) in store.calls


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "excess",
        "root_changed",
        "leaf_changed",
        "wrong_pages",
        "page_count_changed",
        "nonempty_terminal",
    ],
)
def test_overlap_does_not_weaken_completeness_and_stability_guards(
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    store = OverlappingStore(defect=defect)
    store.install(monkeypatch)

    with pytest.raises(FeedFetchError) as error:
        fetch_catalog()

    assert error.value.retryable is True
    # Failures must reach the injected evidence, not reject overlapping probes.
    target = (None, 1) if defect == "root_changed" else ((0, 2), 1)
    assert store.visits[target] >= (3 if defect == "leaf_changed" else 2)
    if defect == "nonempty_terminal":
        assert ((0, 2), 2) in store.calls


def test_short_page_recovered_by_overlapping_shard_completes_catalog(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = OverlappingStore(defect="short_page")
    store.install(monkeypatch)

    products = fetch_catalog()

    assert {product.id for product in products} == set(range(1, 6))
    assert len(products) == 5
    assert "underfilled shard page" in caplog.text
    assert "global unique-ID reconciliation is still required" in caplog.text


def test_short_page_unrecovered_hole_fails_final_unique_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OverlappingStore(defect="short_page")
    store.lower_cards = [1, 3, 2]
    store.install(monkeypatch)

    with pytest.raises(FeedFetchError, match="IncompleteCatalog"):
        fetch_catalog()

    assert ((0, 2), 1) in store.calls
    assert ((2, 4), 1) in store.calls


def test_reaching_root_unique_count_does_not_skip_later_conflicting_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OverlappingStore(49, defect="late_conflict")
    # Lower leaf supplies 1..25; upper page one supplies 25..48. To reach all
    # 49 before page two, put 49 in lower too, preserving known-root membership.
    store.lower_cards = [*range(1, 26), 49]
    store.upper_cards = [*range(25, 49), 25]
    store.install(monkeypatch, cap=26)

    with pytest.raises(FeedFetchError) as error:
        fetch_catalog()

    assert error.value.retryable is True
    assert ((24, 48), 2) in store.calls


def test_recursive_overlapping_children_shrink_and_cover_every_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OverlappingStore(9)
    store.install(monkeypatch)

    products = fetch_catalog()

    assert {p.id for p in products} == set(range(1, 10))
    expected_ranges = {(0, 8), (0, 4), (4, 8), (0, 2), (2, 4), (4, 6), (6, 8)}
    assert {
        bounds for bounds, _ in store.calls if bounds is not None
    } == expected_ranges
    assert len(store.calls) < 32


@pytest.mark.parametrize("wire_width", [1, 2])
def test_dense_unsplittable_wire_interval_fails_in_bounded_work(
    monkeypatch: pytest.MonkeyPatch,
    wire_width: int,
) -> None:
    store = OverlappingStore()
    store.maximum = wire_width - 1
    store.install(monkeypatch)

    with pytest.raises(FeedFetchError, match="PriceBucketLimitExceeded"):
        fetch_catalog()

    assert len(store.calls) == 2


def test_invalid_real_catalog_never_reaches_snapshot_or_delivery_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = OverlappingStore(defect="missing")
    source.install(monkeypatch)
    feed = FeedConfig(
        id="gjirafa50",
        name="Gjirafa50",
        url=ROOT_URL,
        webhook="https://discord.test/webhooks/id/token",
        strategy="gjirafa50",
    )
    previous = PriceSnapshot(feed.id, "1", Decimal(140), "140,00 MKD.", "MKD")
    sender = RecordingSender([])
    prepare_delivery = Mock(side_effect=AssertionError("unexpected delivery planning"))
    monkeypatch.setattr(prices, "prepare_price_delivery", prepare_delivery)

    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshots((previous,))
        load_snapshots = store.load_price_snapshots
        snapshot_load = Mock(wraps=load_snapshots)
        monkeypatch.setattr(store, "load_price_snapshots", snapshot_load)
        monitor = Gjirafa50PriceMonitor(
            feed,
            Gjirafa50PriceMonitorDependencies(
                catalog=Gjirafa50CatalogClient(),
                snapshots=store,
                sender=sender,
                fetch_retry_policy=no_wait_retry_policy(),
                sqlite_retry_policy=SQLiteRetryPolicy(
                    sleep=lambda seconds: True,
                    on_retry=lambda error, delay: None,
                ),
                delivery=PriceAlertDelivery(
                    sleep=lambda seconds: True,
                    delay_between_posts=0,
                    is_shutdown_requested=lambda: False,
                ),
                database_path=store.database_path,
            ),
        )

        with pytest.raises(FeedFetchError, match="IncompleteCatalog") as error:
            monitor.scan()

        assert error.value.retryable is True
        # Both leaves, their sentinels, and post-count checks ran: real products
        # were fetched, but the missing unique ID invalidates the whole scan.
        assert source.visits[((0, 2), 1)] == 3
        assert source.visits[((2, 4), 1)] == 3
        assert ((2, 4), 2) in source.calls
        snapshot_load.assert_not_called()
        prepare_delivery.assert_not_called()
        assert sender.messages == []
        assert load_snapshots(feed.id) == (previous,)
        assert store.load_active_price_batch(feed.id) is None
        store.require_no_open_price_claims(feed.id)
