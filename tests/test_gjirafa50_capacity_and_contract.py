from datetime import datetime
from decimal import Decimal
from math import ceil

import pytest

from rss2discord.providers.gjirafa50 import catalog as gjirafa50_catalog
from rss2discord.providers.gjirafa50.catalog import (
    Gjirafa50CatalogClient,
    _CatalogScan,
    _OperationBudget,
)
from rss2discord.providers.gjirafa50.http import (
    FetchedGjirafa50Page,
    Gjirafa50HttpClient,
    Gjirafa50PageRequest,
)
from rss2discord.providers.gjirafa50.models import (
    Gjirafa50CatalogPage,
    Gjirafa50PriceRange,
    Gjirafa50Product,
)
from rss2discord.providers.gjirafa50.prices import (
    MAX_GJIRAFA50_RETAINED_SNAPSHOTS,
)
from rss2discord.transports import FeedFetchError
from tests.gjirafa50_helpers import (
    ROOT_URL,
    RecordingGet,
    StubResponse,
    catalog_payload,
)


@pytest.mark.parametrize("total", [137_124, 150_000])
def test_complete_large_catalog_fits_bounded_request_and_card_work(
    monkeypatch: pytest.MonkeyPatch,
    total: int,
) -> None:
    # Exercise real sharding, enumeration, and reconciliation without network or
    # HTML allocation. This proves capacity, not live latency/byte feasibility.
    def fetch_page(
        _self: Gjirafa50HttpClient,
        root_url: str,
        request: Gjirafa50PageRequest,
        observed_at: datetime,
    ) -> FetchedGjirafa50Page:
        request.budget.before_request()
        request.budget.consume_bytes(1_024)
        low, high = 1, total + 1
        if request.price_range is not None:
            low = min(high, max(low, request.price_range.minimum_cents))
            high = min(high, max(low, request.price_range.maximum_exclusive_cents))
        count = high - low
        start = low + (request.page - 1) * 24
        products = tuple(
            Gjirafa50Product(
                id=product_id,
                title="Synthetic product",
                link=root_url,
                image_url=None,
                price=Decimal(product_id) / 100,
                currency="EUR",
                formatted_price=str(product_id),
                observed_at=observed_at,
            )
            for product_id in range(start, min(high, start + 24))
        )
        return FetchedGjirafa50Page(
            Gjirafa50CatalogPage(count, ceil(count / 24), products),
            1_024,
        )

    monkeypatch.setattr(Gjirafa50HttpClient, "fetch_page", fetch_page)
    budget = _OperationBudget(lambda: False)
    with Gjirafa50HttpClient(RecordingGet([])) as http:
        products = Gjirafa50CatalogClient()._scan_catalog(ROOT_URL, budget, http)
    assert len(products) == len({product.id for product in products}) == total
    assert total < budget.products <= gjirafa50_catalog.MAX_GJIRAFA50_FETCHED_PRODUCTS
    assert budget.requests <= gjirafa50_catalog.MAX_GJIRAFA50_PAGES
    assert total <= MAX_GJIRAFA50_RETAINED_SNAPSHOTS


def test_unique_capacity_is_independent_of_repeated_card_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_PRODUCTS", 2)
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_FETCHED_PRODUCTS", 6)
    budget = _OperationBudget(lambda: False)
    budget.consume_products(2)
    budget.consume_products(2)
    budget.consume_products(2)
    with pytest.raises(FeedFetchError, match="ProductWorkLimitExceeded"):
        budget.consume_products(1)


def test_root_over_unique_capacity_fails_before_enumeration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    get = RecordingGet(
        [StubResponse(catalog_payload(150_001, [(1, Decimal(10))]))],
    )
    with (
        Gjirafa50HttpClient(get) as http,
        pytest.raises(FeedFetchError, match="ProductLimitExceeded"),
    ):
        Gjirafa50CatalogClient()._scan_catalog(
            ROOT_URL,
            _OperationBudget(lambda: False),
            http,
        )
    assert len(get.params) == 1
    assert "reported=150001 limit=150000" in caplog.text


@pytest.mark.parametrize(
    ("card_count", "outlier", "error", "diagnostic"),
    [
        (23, False, "IncompleteCatalog", "expected=24 rendered=23"),
        (24, True, "PriceOutsideShard", "price_cents=2079000"),
    ],
)
def test_observed_mk_anomalies_remain_fail_closed(
    caplog: pytest.LogCaptureFixture,
    card_count: int,
    *,
    outlier: bool,
    error: str,
    diagnostic: str,
) -> None:
    products = [
        (i + 1, Decimal(20_790 if outlier and i == 0 else 12_000))
        for i in range(card_count)
    ]
    get = RecordingGet(
        [StubResponse(catalog_payload(5_873, products, total_pages=245))],
    )
    collected: list[Gjirafa50Product] = []
    seen: set[int] = set()
    with Gjirafa50HttpClient(get) as http:
        scan = _CatalogScan(
            "https://gjirafa50.mk/",
            _OperationBudget(lambda: False),
            http,
        )
        with pytest.raises(FeedFetchError, match=error):
            Gjirafa50CatalogClient()._scan_shard(
                scan,
                Gjirafa50PriceRange(1_000_000, 1_999_901),
                5_873,
                collected,
                seen,
            )
    assert collected == []
    assert seen == set()
    assert diagnostic in caplog.text
    assert "unverified" in caplog.text
    assert "https://" not in caplog.text


def test_price_outlier_diagnostic_identifies_product_and_displayed_mk_amount(
    caplog: pytest.LogCaptureFixture,
) -> None:
    get = RecordingGet(
        [
            StubResponse(
                catalog_payload(
                    5_873,
                    [
                        (489_034, Decimal(20_790)),
                        *((product_id, Decimal(12_000)) for product_id in range(1, 24)),
                    ],
                    total_pages=245,
                ),
            ),
        ],
    )
    with Gjirafa50HttpClient(get) as http:
        scan = _CatalogScan(
            "https://gjirafa50.mk/",
            _OperationBudget(lambda: False),
            http,
        )
        with pytest.raises(FeedFetchError, match="PriceOutsideShard"):
            Gjirafa50CatalogClient()._scan_shard(
                scan,
                Gjirafa50PriceRange(1_000_000, 1_999_901),
                5_873,
                [],
                set(),
            )

    assert (
        "phase=enumeration page=1 product_id=489034 "
        "expected_range=10000-19999 displayed_amount=20.790,00 MKD. "
        "currency=MKD price_cents=2079000"
    ) in caplog.text
    assert "https://" not in caplog.text


def test_overlapping_price_filter_counts_log_parent_and_child_ranges(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gjirafa50_catalog,
        "MAX_GJIRAFA50_PRICE_EXCLUSIVE_CENTS",
        3_199,
    )
    responses = [
        catalog_payload(65_616, []),
        catalog_payload(38_790, []),
        catalog_payload(26_828, []),
    ]
    get = RecordingGet([StubResponse(payload) for payload in responses])
    with Gjirafa50HttpClient(get) as http:
        scan = _CatalogScan(
            "https://gjirafa50.com/",
            _OperationBudget(lambda: False),
            http,
        )
        with pytest.raises(FeedFetchError, match="CatalogChanged"):
            Gjirafa50CatalogClient()._build_shards(scan, 65_616)

    assert (
        "phase=split parent_range=0-31,98 parent_count=65616 "
        "lower_range=0-15,98 lower_count=38790 "
        "upper_range=15,99-31,98 upper_count=26828 child_sum=65618"
    ) in caplog.text
    assert "may overlap" in caplog.text
    assert "https://" not in caplog.text


def test_distinct_cent_filters_can_return_identical_boundary_products() -> None:
    products = [(489_034, Decimal(16)), (488_476, Decimal(16))]
    payload = catalog_payload(2, products)
    get = RecordingGet(
        [
            StubResponse(payload),
            StubResponse(payload),
        ],
    )
    observed_at = datetime.now().astimezone()
    with Gjirafa50HttpClient(get) as http:
        first = http.fetch_page(
            "https://gjirafa50.com/",
            Gjirafa50PageRequest(
                1,
                _OperationBudget(lambda: False),
                Gjirafa50PriceRange(1_598, 1_599),
            ),
            observed_at,
        ).page
        second = http.fetch_page(
            "https://gjirafa50.com/",
            Gjirafa50PageRequest(
                1,
                _OperationBudget(lambda: False),
                Gjirafa50PriceRange(1_599, 1_600),
            ),
            observed_at,
        ).page

    assert get.params[0]["price"] == "15,98-15,98"
    assert get.params[1]["price"] == "15,99-15,99"
    assert "." not in str(get.params[0]["price"])
    assert "." not in str(get.params[1]["price"])
    expected_ids = [489_034, 488_476]
    assert [product.id for product in first.products] == expected_ids
    assert [product.id for product in second.products] == expected_ids
    assert all(
        product.price == Decimal(16) for product in (*first.products, *second.products)
    )
    assert all(
        product.currency == "EUR" for product in (*first.products, *second.products)
    )


def test_overlapping_out_of_shard_results_fail_without_partial_inventory() -> None:
    payload = catalog_payload(
        2,
        [(489_034, Decimal(16)), (488_476, Decimal(16))],
    )
    get = RecordingGet([StubResponse(payload), StubResponse(payload)])
    price_ranges = (
        Gjirafa50PriceRange(1_598, 1_599),
        Gjirafa50PriceRange(1_599, 1_600),
    )

    with Gjirafa50HttpClient(get) as http:
        for price_range in price_ranges:
            collected: list[Gjirafa50Product] = []
            seen: set[int] = set()
            scan = _CatalogScan(
                "https://gjirafa50.com/",
                _OperationBudget(lambda: False),
                http,
            )
            with pytest.raises(FeedFetchError, match="PriceOutsideShard"):
                Gjirafa50CatalogClient()._scan_shard(
                    scan,
                    price_range,
                    2,
                    collected,
                    seen,
                )
            assert collected == []
            assert seen == set()

    assert [params["price"] for params in get.params] == [
        "15,98-15,98",
        "15,99-15,99",
    ]


def test_larger_catalog_capacity_keeps_byte_and_time_ceilings() -> None:
    budget = _OperationBudget(lambda: False)
    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        budget.consume_bytes(gjirafa50_catalog.MAX_GJIRAFA50_SCAN_BYTES + 1)
    budget.deadline = 0
    with pytest.raises(FeedFetchError, match="ScanTimeLimitExceeded"):
        budget.check_active()
