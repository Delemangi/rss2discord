from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from itertools import pairwise

import pytest
import requests
from pydantic import JsonValue

from rss2discord.transports.setec_catalog import SetecCatalogClient
from rss2discord.transports.setec_catalog_bounds import (
    MAX_SETEC_SEARCH_REQUESTS,
    SETEC_PRICE_FIELD,
)
from tests.setec_helpers import (
    CATALOG_URL,
    FakeMeilisearchIndex,
    IndexedProduct,
    catalog_scan_should_stop,
    no_wait_fetch_retry_policy,
)

HEAVY_TIE_COUNTS = {49: 900, 499: 850, 999: 800, 1_999: 950, 4_999: 700}
PRODUCTION_SCALE_PRODUCT_FLOOR = 12_750
DISTINCT_AMOUNT_FLOOR = 2_000
LONG_TAIL_AMOUNT = 1_500_000
FORMER_CATALOG_PAGE_SIZE = 250
FORMER_CATALOG_PAGE_COUNT = 54
DOCUMENT_FETCH_BUDGET = 40
MINIMUM_DOCUMENT_FETCHES = 1
REQUEST_BUDGET_HEADROOM = 10


def production_scale_corpus() -> tuple[IndexedProduct, ...]:
    """Build a wide, skewed catalogue with heavy ties and a long premium tail."""
    products: list[IndexedProduct] = []

    def add(amount: int, count: int) -> None:
        for _ in range(count):
            products.append(IndexedProduct(f"prod-{len(products):05d}", amount))

    for tie_amount, tie_count in HEAVY_TIE_COUNTS.items():
        add(tie_amount, tie_count)
    for amount in range(50, 1_000):
        if amount not in HEAVY_TIE_COUNTS:
            add(amount, 2)
    for amount in range(1_000, 10_000, 10):
        add(amount, 6)
    for amount in range(10_000, 100_000, 250):
        add(amount, 3)
    for amount in range(100_000, 500_000, 2_500):
        add(amount, 3)
    for amount in range(500_000, 1_500_001, 12_500):
        add(amount, 4)
    return tuple(products)


def document_fetch_bands(
    bodies: Sequence[Mapping[str, JsonValue]],
) -> list[tuple[float, float]]:
    """Return the half-open band of every document fetch, in request order."""
    bands: list[tuple[float, float]] = []
    for body in bodies:
        clauses = body.get("filter")
        if body.get("limit") == 0 or not isinstance(clauses, list):
            continue
        low = 0.0
        high = float("inf")
        for clause in clauses:
            if not isinstance(clause, str) or not clause.startswith(SETEC_PRICE_FIELD):
                continue
            if ">=" in clause:
                low = float(clause.rsplit(">=", 1)[1])
            else:
                high = float(clause.rsplit("<", 1)[1])
        bands.append((low, high))
    return bands


def test_setec_catalog_client_recovers_every_product_of_a_production_scale_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    corpus = production_scale_corpus()
    index = FakeMeilisearchIndex(corpus)
    monkeypatch.setattr(requests, "post", index)

    # When
    entries = SetecCatalogClient().fetch_price_index(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert len(corpus) >= PRODUCTION_SCALE_PRODUCT_FLOOR
    assert len({item.amount for item in corpus}) >= DISTINCT_AMOUNT_FLOOR
    assert max(item.numeric_amount for item in corpus) >= LONG_TAIL_AMOUNT
    assert len(entries) == len(corpus)
    assert Counter(entry.id for entry in entries) == Counter(
        item.product_id for item in corpus
    )
    assert {entry.id: entry.calculated_amount for entry in entries} == {
        item.product_id: Decimal(str(item.amount)) for item in corpus
    }
    recovered_amounts = Counter(str(entry.calculated_amount) for entry in entries)
    assert {
        tie_amount: recovered_amounts[str(tie_amount)]
        for tie_amount in HEAVY_TIE_COUNTS
    } == HEAVY_TIE_COUNTS


def test_setec_catalog_client_traverses_a_production_scale_catalog_within_the_request_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    corpus = production_scale_corpus()
    index = FakeMeilisearchIndex(corpus)
    monkeypatch.setattr(requests, "post", index)

    # When
    entries = SetecCatalogClient().fetch_price_index(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert len(entries) == len(corpus)
    assert len(corpus) > FORMER_CATALOG_PAGE_SIZE * (FORMER_CATALOG_PAGE_COUNT - 1)
    assert len(corpus) <= FORMER_CATALOG_PAGE_SIZE * FORMER_CATALOG_PAGE_COUNT
    assert len(index.bodies) * REQUEST_BUDGET_HEADROOM <= MAX_SETEC_SEARCH_REQUESTS
    assert index.hit_request_total <= DOCUMENT_FETCH_BUDGET
    assert index.hit_request_total < FORMER_CATALOG_PAGE_COUNT
    assert index.hit_request_total >= MINIMUM_DOCUMENT_FETCHES
    # Every request is either a facet count or a document fetch by shape, never a
    # count that also drags documents back nor a fetch with no projection.
    assert all(
        ("facets" in body) != ("attributesToRetrieve" in body) for body in index.bodies
    )


def test_setec_catalog_client_fetches_production_scale_documents_in_ascending_disjoint_bands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    corpus = production_scale_corpus()
    index = FakeMeilisearchIndex(corpus)
    monkeypatch.setattr(requests, "post", index)

    # When
    entries = SetecCatalogClient().fetch_price_index(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    bands = document_fetch_bands(index.bodies)
    assert len(bands) == index.hit_request_total
    assert all(earlier[1] <= later[0] for earlier, later in pairwise(bands))
    assert bands[0][0] == 0.0
    assert bands[-1][1] == max(item.numeric_amount for item in corpus) + 1.0
    assert len(entries) == len(corpus)
