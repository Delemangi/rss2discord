import json
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from decimal import Decimal

import pytest
import requests
from pydantic import JsonValue

from rss2discord.retries import (
    FETCH_MAX_ATTEMPTS,
    FeedFetchInterruptedError,
    FetchRetryPolicy,
)
from rss2discord.transports import FeedFetchError, setec_catalog, setec_catalog_bounds
from rss2discord.transports.setec_catalog import SetecCatalogClient
from rss2discord.transports.setec_catalog_bounds import (
    MAX_SETEC_BAND_DEPTH,
    SETEC_COUNT_FIELD,
    SETEC_PRICE_FIELD,
    SETEC_PRICE_PROJECTION,
    SETEC_PRODUCT_LOOKUP_BATCH_SIZE,
    SETEC_SEARCH_PAGE_SIZE,
    SETEC_SEARCH_URL,
)
from tests.setec_helpers import (
    CATALOG_URL,
    FakeMeilisearchIndex,
    IndexedProduct,
    RaisingPost,
    RecordingPost,
    StubResponse,
    catalog_scan_should_stop,
    count_payload,
    no_wait_fetch_retry_policy,
    price_payload,
    product_payload,
    search_payload,
)

SMALL_PAGE_SIZE = 2
BOUNDARY_PAGE_SIZE = 4
PRODUCT_LOOKUP_ID_COUNT = SETEC_PRODUCT_LOOKUP_BATCH_SIZE * 2 + 50
SMALL_LOOKUP_BATCH_SIZE = 2
RAW_MAXIMUM_PLACEHOLDER = "__raw_maximum__"

# Spelled out rather than derived from the shipped constant, so a projection field
# that silently disappears from the request is caught instead of followed.
DISPLAY_PROJECTION_FIELDS = [
    "id",
    "title",
    "handle",
    "thumbnail",
    "created_at",
    "product_categories.name",
    "variants.calculated_price.calculated_amount",
    "variants.calculated_price.original_amount",
    "variants.calculated_price.currency_code",
]
UNFILTERED_HITS_BODY: Mapping[str, JsonValue] = {
    "q": "",
    "limit": SETEC_SEARCH_PAGE_SIZE,
}
SUMMARY_WITHOUT_ANY_FACET_DISTRIBUTION = json.dumps(
    {"hits": [], "estimatedTotalHits": 0, "facetStats": {}},
).encode()
SUMMARY_WITHOUT_THE_COUNT_FACET = json.dumps(
    {"hits": [], "facetDistribution": {"other": {"published": 2}}},
).encode()
SUMMARY_COUNTING_NOTHING_WHILE_PRICING_SOMETHING = json.dumps(
    {
        "hits": [],
        "facetDistribution": {SETEC_COUNT_FIELD: {}, SETEC_PRICE_FIELD: {}},
        "facetStats": {SETEC_PRICE_FIELD: {"min": 1_499.0, "max": 2_999.0}},
    },
).encode()

BAND_HITS_SHORT_OF_ITS_FACET_COUNT = [
    StubResponse(count_payload([1_499, 2_999])),
    StubResponse(count_payload([1_499, 2_999])),
    StubResponse(search_payload([price_payload("prod-1", price=1_499)])),
]
SCAN_ENTRIES_SHORT_OF_THE_DECLARED_TOTAL = [
    StubResponse(count_payload([1_499, 2_999, 4_999])),
    StubResponse(count_payload([1_499, 2_999])),
    StubResponse(
        search_payload(
            [
                price_payload("prod-1", price=1_499),
                price_payload("prod-2", price=2_999),
            ],
        ),
    ),
]


def band_filter(low: float, high: float | None = None) -> list[str]:
    """Rebuild the filter clauses the scan sends for one half-open band."""
    if high is None:
        return [f"{SETEC_PRICE_FIELD} >= {low}"]
    return [f"{SETEC_PRICE_FIELD} >= {low}", f"{SETEC_PRICE_FIELD} < {high}"]


def use_page_size(monkeypatch: pytest.MonkeyPatch, page_size: int) -> None:
    """Shrink the per-search page size the bisection splits on."""
    monkeypatch.setattr(setec_catalog, "SETEC_SEARCH_PAGE_SIZE", page_size)


def use_lookup_batch_size(monkeypatch: pytest.MonkeyPatch, batch_size: int) -> None:
    """Shrink the identifier batch size the display lookup chunks on."""
    monkeypatch.setattr(setec_catalog, "SETEC_PRODUCT_LOOKUP_BATCH_SIZE", batch_size)


def summary_payload_with_raw_maximum(raw_maximum: str) -> bytes:
    """Encode a populated summary carrying the given raw JSON price maximum."""
    payload = json.dumps(
        {
            "hits": [],
            "facetDistribution": {
                SETEC_COUNT_FIELD: {"published": 2},
                SETEC_PRICE_FIELD: {"1499": 1, "2999": 1},
            },
            "facetStats": {
                SETEC_PRICE_FIELD: {"min": 1_499.0, "max": RAW_MAXIMUM_PLACEHOLDER},
            },
        },
    )
    return payload.replace(f'"{RAW_MAXIMUM_PLACEHOLDER}"', raw_maximum).encode()


def whole_catalog_count_payload(
    priced_amounts: Sequence[float],
    *,
    unpriced_total: int,
) -> bytes:
    """Encode a summary counting every document but pricing only the priced ones."""
    return json.dumps(
        {
            "hits": [],
            "facetDistribution": {
                SETEC_COUNT_FIELD: {
                    "published": len(priced_amounts) + unpriced_total,
                },
                SETEC_PRICE_FIELD: dict(
                    Counter(str(amount) for amount in priced_amounts),
                ),
            },
            "facetStats": {
                SETEC_PRICE_FIELD: {
                    "min": min(priced_amounts),
                    "max": max(priced_amounts),
                },
            },
        },
    ).encode()


def hit_without_a_title(product_id: str, handle: str) -> dict[str, JsonValue]:
    """Build a display hit whose absent title makes it unusable."""
    hit = product_payload(product_id, handle)
    del hit["title"]
    return hit


def hit_with_a_null_original_amount(
    product_id: str,
    handle: str,
) -> dict[str, JsonValue]:
    """Build a display hit whose null original amount makes it unusable."""
    return {
        "id": product_id,
        "title": f"Product {product_id}",
        "handle": handle,
        "created_at": "2026-07-23T02:24:28.424Z",
        "variants": [
            {
                "calculated_price": {
                    "calculated_amount": 1_499,
                    "original_amount": None,
                    "currency_code": "mkd",
                },
            },
        ],
    }


def served_identifiers(payload: bytes) -> list[str]:
    """Return the identifier of every hit in an encoded search response."""
    hits = json.loads(payload)["hits"]
    assert isinstance(hits, list)
    return [hit["id"] for hit in hits]


def narrows_on_price(body: Mapping[str, JsonValue]) -> bool:
    """Report whether a search body constrains the calculated amount."""
    clauses = body.get("filter")
    if not isinstance(clauses, list):
        return False
    return any(
        isinstance(clause, str) and clause.startswith(SETEC_PRICE_FIELD)
        for clause in clauses
    )


def requested_identifiers(body: Mapping[str, JsonValue]) -> list[str]:
    """Return the identifiers one display-lookup body selects, in request order."""
    search_filter = body["filter"]
    assert isinstance(search_filter, list)
    clause = search_filter[0]
    assert isinstance(clause, str)
    assert clause.startswith("id IN [")
    identifiers = json.loads(clause.removeprefix("id IN "))
    assert isinstance(identifiers, list)
    return identifiers


class CatalogWithUnpricedProducts:
    """Serve a corpus in which some documents carry no usable price at all.

    A document whose price is missing or not a number cannot satisfy a numeric
    price filter, so a query narrowing on the calculated amount sees only the
    priced part of the catalogue, as the live index behaves. A query carrying no
    such clause sees the unpriced documents too, so a scan that ever dropped the
    price filter would have to face them.
    """

    def __init__(
        self,
        priced: Sequence[IndexedProduct],
        unpriced: Sequence[Mapping[str, JsonValue]],
    ) -> None:
        self.priced_ids: tuple[str, ...] = tuple(item.product_id for item in priced)
        self.unpriced_ids: tuple[str, ...] = tuple(
            str(document["id"]) for document in unpriced
        )
        self.bodies: list[Mapping[str, JsonValue]] = []
        self._priced = tuple(priced)
        self._unpriced = tuple(dict(document) for document in unpriced)
        self._index = FakeMeilisearchIndex(priced)

    def __call__(
        self,
        url: str,
        *,
        json: Mapping[str, JsonValue],
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
        allow_redirects: bool,
    ) -> AbstractContextManager[StubResponse]:
        del url, headers, timeout, stream, allow_redirects
        self.bodies.append(json)
        return nullcontext(StubResponse(self.payload_for(json)))

    def payload_for(self, body: Mapping[str, JsonValue]) -> bytes:
        """Answer one request, hiding unpriced documents only from a priced query."""
        if narrows_on_price(body):
            return self._priced_payload(body)
        if body.get("limit") == 0 and "facets" in body:
            return whole_catalog_count_payload(
                [item.numeric_amount for item in self._priced],
                unpriced_total=len(self._unpriced),
            )
        return search_payload(
            [
                *(
                    price_payload(item.product_id, price=item.amount)
                    for item in self._priced
                ),
                *self._unpriced,
            ],
        )

    @property
    def filters(self) -> list[JsonValue]:
        return [body.get("filter") for body in self.bodies]

    def _priced_payload(self, body: Mapping[str, JsonValue]) -> bytes:
        with self._index(
            SETEC_SEARCH_URL,
            json=body,
            headers={},
            timeout=0,
            stream=True,
            allow_redirects=False,
        ) as response:
            return response.content


def test_setec_catalog_client_enumerates_every_priced_product_in_ascending_band_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            StubResponse(count_payload([1_499, 2_999])),
            StubResponse(count_payload([1_499, 2_999])),
            StubResponse(
                search_payload(
                    [
                        price_payload("prod-1", price=1_499),
                        price_payload("prod-2", price=2_999),
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When
    entries = SetecCatalogClient().fetch_price_index(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [entry.id for entry in entries] == ["prod-1", "prod-2"]
    assert [entry.calculated_amount for entry in entries] == [
        Decimal(1_499),
        Decimal(2_999),
    ]
    assert post.urls == [SETEC_SEARCH_URL] * 3
    assert post.bodies[0] == {
        "q": "",
        "limit": 0,
        "filter": band_filter(0.0),
        "facets": [SETEC_COUNT_FIELD, SETEC_PRICE_FIELD],
    }
    assert post.bodies[1] == {
        "q": "",
        "limit": 0,
        "filter": band_filter(0.0, 3_000.0),
        "facets": [SETEC_COUNT_FIELD],
    }
    assert post.bodies[2] == {
        "q": "",
        "limit": SETEC_SEARCH_PAGE_SIZE,
        "filter": band_filter(0.0, 3_000.0),
        "attributesToRetrieve": list(SETEC_PRICE_PROJECTION),
    }


def test_setec_catalog_client_collapses_identical_product_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    duplicate = price_payload("prod-1", price=1_499)
    post = RecordingPost(
        [
            StubResponse(count_payload([1_499])),
            StubResponse(count_payload([1_499, 1_499])),
            StubResponse(search_payload([duplicate, duplicate])),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When
    entries = SetecCatalogClient().fetch_price_index(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [entry.id for entry in entries] == ["prod-1"]
    assert [entry.calculated_amount for entry in entries] == [Decimal(1_499)]


def test_setec_catalog_client_restarts_then_fails_on_conflicting_product_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    # One product reported at two amounts is how a price edited mid-scan looks, so
    # the scan restarts from the summary rather than failing on the first sighting.
    conflicting_attempt = [
        StubResponse(count_payload([1_499, 2_999])),
        StubResponse(count_payload([1_499, 2_999])),
        StubResponse(
            search_payload(
                [
                    price_payload("prod-1", price=1_499),
                    price_payload("prod-1", price=2_999),
                ],
            ),
        ),
    ]
    post = RecordingPost(conflicting_attempt * FETCH_MAX_ATTEMPTS)
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="DuplicateProductId") as raised:
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    assert raised.value.retryable is True
    assert len(post.bodies) == len(conflicting_attempt) * FETCH_MAX_ATTEMPTS


def test_setec_catalog_client_stops_before_a_later_band_when_shutdown_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    index = FakeMeilisearchIndex(
        [
            IndexedProduct("prod-low", 10.0),
            IndexedProduct("prod-mid", 20.0),
            IndexedProduct("prod-high", 30.0),
        ],
        page_size=SMALL_PAGE_SIZE,
    )
    monkeypatch.setattr(requests, "post", index)
    use_page_size(monkeypatch, SMALL_PAGE_SIZE)
    requests_before_the_upper_band = 4

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=lambda: (
                len(index.bodies) >= requests_before_the_upper_band
            ),
        )

    # Then
    assert len(index.bodies) == requests_before_the_upper_band
    assert band_filter(15.5, 31.0) not in index.filters


def test_setec_catalog_client_restarts_the_complete_scan_after_a_retryable_band_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    hits = search_payload(
        [price_payload("prod-1", price=1_499), price_payload("prod-2", price=2_999)],
    )
    post = RecordingPost(
        [
            StubResponse(count_payload([1_499, 2_999])),
            StubResponse(count_payload([1_499, 2_999])),
            StubResponse(b"retry me", status_code=503),
            StubResponse(count_payload([1_499, 2_999])),
            StubResponse(count_payload([1_499, 2_999])),
            StubResponse(hits),
        ],
    )
    monkeypatch.setattr(requests, "post", post)
    retry_delays: list[float] = []

    def record_retry_sleep(seconds: float) -> bool:
        retry_delays.append(seconds)
        return True

    retry_policy = FetchRetryPolicy(
        sleep=record_retry_sleep,
        on_retry=lambda error, delay: None,
    )

    # When
    entries = SetecCatalogClient().fetch_price_index(
        CATALOG_URL,
        retry_policy=retry_policy,
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [entry.id for entry in entries] == ["prod-1", "prod-2"]
    assert post.filters == [
        band_filter(0.0),
        band_filter(0.0, 3_000.0),
        band_filter(0.0, 3_000.0),
        band_filter(0.0),
        band_filter(0.0, 3_000.0),
        band_filter(0.0, 3_000.0),
    ]
    assert len(retry_delays) == 1


def test_bisection_splits_a_partition_that_exceeds_the_per_search_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    index = FakeMeilisearchIndex(
        [
            IndexedProduct("prod-low", 10.0),
            IndexedProduct("prod-mid", 20.0),
            IndexedProduct("prod-high", 30.0),
        ],
        page_size=SMALL_PAGE_SIZE,
    )
    monkeypatch.setattr(requests, "post", index)
    use_page_size(monkeypatch, SMALL_PAGE_SIZE)

    # When
    entries = SetecCatalogClient().fetch_price_index(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [entry.id for entry in entries] == ["prod-low", "prod-mid", "prod-high"]
    assert index.filters == [
        band_filter(0.0),
        band_filter(0.0, 31.0),
        band_filter(0.0, 15.5),
        band_filter(0.0, 15.5),
        band_filter(15.5, 31.0),
        band_filter(15.5, 31.0),
    ]
    assert index.hit_request_total == 2


def test_bisection_partitions_are_half_open_so_boundary_products_are_neither_dropped_nor_doubled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    corpus = [
        IndexedProduct("prod-at-lower-bound", 0.0),
        IndexedProduct("prod-below-midpoint", 25.0),
        IndexedProduct("prod-on-midpoint", 50.0),
        IndexedProduct("prod-also-on-midpoint", 50.0),
        IndexedProduct("prod-above-midpoint", 75.0),
        IndexedProduct("prod-at-upper-bound", 99.0),
    ]
    index = FakeMeilisearchIndex(corpus, page_size=BOUNDARY_PAGE_SIZE)
    monkeypatch.setattr(requests, "post", index)
    use_page_size(monkeypatch, BOUNDARY_PAGE_SIZE)

    # When
    entries = SetecCatalogClient().fetch_price_index(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert Counter(entry.id for entry in entries) == Counter(
        item.product_id for item in corpus
    )
    assert index.filters == [
        band_filter(0.0),
        band_filter(0.0, 100.0),
        band_filter(0.0, 50.0),
        band_filter(0.0, 50.0),
        band_filter(50.0, 100.0),
        band_filter(50.0, 100.0),
    ]
    assert index.hit_request_total == 2


def test_bisection_terminates_when_many_products_share_one_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    index = FakeMeilisearchIndex(
        [
            IndexedProduct("prod-1", 1_000.0),
            IndexedProduct("prod-2", 1_000.0),
            IndexedProduct("prod-3", 1_000.0),
        ],
        page_size=SMALL_PAGE_SIZE,
    )
    monkeypatch.setattr(requests, "post", index)
    use_page_size(monkeypatch, SMALL_PAGE_SIZE)

    # When / Then
    with pytest.raises(FeedFetchError, match="PriceBandUnsplittable") as raised:
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    assert raised.value.retryable is False
    assert index.count_request_total <= 2 * MAX_SETEC_BAND_DEPTH + 2
    assert index.hit_request_total == 0


def test_products_with_missing_or_non_numeric_price_are_omitted_from_the_price_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    index = CatalogWithUnpricedProducts(
        [IndexedProduct("prod-1", 1_499), IndexedProduct("prod-2", 2_999)],
        [
            {"id": "prod-without-a-price", "variants": []},
            {
                "id": "prod-with-a-non-numeric-price",
                "variants": [
                    {
                        "calculated_price": {
                            "calculated_amount": "on request",
                            "currency_code": "mkd",
                        },
                    },
                ],
            },
        ],
    )
    monkeypatch.setattr(requests, "post", index)

    # When
    entries = SetecCatalogClient().fetch_price_index(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    # The summary itself has to carry the amount clause, because the fixture only
    # withholds the unpriced documents from a request that narrows on the amount.
    assert index.filters[0] == band_filter(0.0)
    assert set(index.unpriced_ids) <= set(
        served_identifiers(index.payload_for(UNFILTERED_HITS_BODY)),
    )
    assert [entry.id for entry in entries] == list(index.priced_ids)
    assert set(index.unpriced_ids).isdisjoint(entry.id for entry in entries)
    assert all(
        isinstance(clause, str) and clause.startswith(SETEC_PRICE_FIELD)
        for search_filter in index.filters
        if isinstance(search_filter, list)
        for clause in search_filter
    )


@pytest.mark.parametrize(
    "attempt_responses",
    [BAND_HITS_SHORT_OF_ITS_FACET_COUNT, SCAN_ENTRIES_SHORT_OF_THE_DECLARED_TOTAL],
    ids=["band_returns_fewer_hits_than_its_facet_count", "scan_misses_declared_total"],
)
def test_facet_total_mismatch_between_parent_and_children_is_retryable(
    attempt_responses: list[StubResponse],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(list(attempt_responses) * 3)
    monkeypatch.setattr(requests, "post", post)
    retry_errors: list[FeedFetchError] = []
    retry_policy = FetchRetryPolicy(
        sleep=lambda seconds: True,
        on_retry=lambda error, delay: retry_errors.append(error),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="IncompleteCatalog") as raised:
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=retry_policy,
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    assert raised.value.retryable is True
    assert [error.cause_type for error in retry_errors] == ["IncompleteCatalog"] * 2
    assert len(post.bodies) == len(attempt_responses) * 3
    assert (
        post.filters[: len(attempt_responses)]
        == post.filters[len(attempt_responses) : 2 * len(attempt_responses)]
    )


def test_setec_catalog_client_reports_an_empty_catalog_from_the_summary_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost([StubResponse(count_payload([]))])
    monkeypatch.setattr(requests, "post", post)

    # When
    entries = SetecCatalogClient().fetch_price_index(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert entries == ()
    assert post.bodies == [
        {
            "q": "",
            "limit": 0,
            "filter": band_filter(0.0),
            "facets": [SETEC_COUNT_FIELD, SETEC_PRICE_FIELD],
        },
    ]


@pytest.mark.parametrize(
    "summary",
    [
        SUMMARY_WITHOUT_ANY_FACET_DISTRIBUTION,
        SUMMARY_WITHOUT_THE_COUNT_FACET,
        SUMMARY_COUNTING_NOTHING_WHILE_PRICING_SOMETHING,
    ],
    ids=[
        "no_facet_distribution_at_all",
        "facet_distribution_without_the_count_field",
        "zero_count_contradicted_by_price_bounds",
    ],
)
def test_setec_catalog_client_rejects_a_summary_whose_count_cannot_be_trusted(
    summary: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    # An uncounted summary must fail closed: reading it as an empty catalogue
    # would silently report every product as gone.
    post = RecordingPost([StubResponse(summary)])
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse") as raised:
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    assert raised.value.retryable is False
    assert len(post.urls) == 1


@pytest.mark.parametrize(
    "raw_maximum",
    ['"NaN"', "1e400"],
    ids=["not_a_number", "beyond_the_float_range"],
)
def test_setec_catalog_client_rejects_price_bounds_that_are_not_finite(
    raw_maximum: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    # A non-finite maximum would widen the first band to an interval that can
    # never be bisected, so it is refused before the traversal starts. The band
    # responses a believed maximum would consume are queued behind it, so a scan
    # that swallowed the bounds would finish rather than run out of answers.
    post = RecordingPost(
        [
            StubResponse(summary_payload_with_raw_maximum(raw_maximum)),
            StubResponse(count_payload([1_499, 2_999])),
            StubResponse(
                search_payload(
                    [
                        price_payload("prod-1", price=1_499),
                        price_payload("prod-2", price=2_999),
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse") as raised:
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    assert raised.value.retryable is False
    assert len(post.urls) == 1


def test_setec_catalog_client_refuses_a_request_once_the_scan_byte_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    # The summary alone consumes the whole budget exactly, so the next request is
    # refused before it is issued rather than while its body streams in.
    summary_count = count_payload([1_499, 2_999])
    post = RecordingPost(
        [StubResponse(summary_count), StubResponse(count_payload([1_499, 2_999]))],
    )
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(
        setec_catalog_bounds,
        "MAX_SETEC_CATALOG_SCAN_BYTES",
        len(summary_count),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    assert len(post.urls) == 1


def test_setec_catalog_client_requests_display_products_in_bounded_id_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product_ids = [f"prod-{number:04d}" for number in range(PRODUCT_LOOKUP_ID_COUNT)]
    batches = [
        product_ids[start : start + SETEC_PRODUCT_LOOKUP_BATCH_SIZE]
        for start in range(0, len(product_ids), SETEC_PRODUCT_LOOKUP_BATCH_SIZE)
    ]
    post = RecordingPost(
        [
            StubResponse(
                search_payload(
                    [
                        product_payload(product_id, f"product-{product_id}")
                        for product_id in batch
                    ],
                ),
            )
            for batch in batches
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When
    products = SetecCatalogClient().fetch_products_by_ids(
        CATALOG_URL,
        product_ids,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    requested_batches = [requested_identifiers(body) for body in post.bodies]
    assert [product.id for product in products] == product_ids
    assert len(requested_batches) == len(batches)
    assert all(
        0 < len(batch) <= SETEC_PRODUCT_LOOKUP_BATCH_SIZE for batch in requested_batches
    )
    assert [
        product_id for batch in requested_batches for product_id in batch
    ] == product_ids
    assert [body["limit"] for body in post.bodies] == [
        len(batch) for batch in requested_batches
    ]
    assert post.bodies[0]["filter"] == [
        "id IN [" + ",".join(f'"{product_id}"' for product_id in batches[0]) + "]",
    ]

    # When
    monkeypatch.setattr(requests, "post", RaisingPost(requests.ConnectionError()))
    empty_lookup = SetecCatalogClient().fetch_products_by_ids(
        CATALOG_URL,
        [],
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert empty_lookup == ()


def test_setec_catalog_client_asks_for_the_display_projection_of_one_looked_up_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [StubResponse(search_payload([product_payload("prod-1", "product-1")]))],
    )
    monkeypatch.setattr(requests, "post", post)

    # When
    products = SetecCatalogClient().fetch_products_by_ids(
        CATALOG_URL,
        ["prod-1"],
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == ["prod-1"]
    assert post.bodies == [
        {
            "q": "",
            "limit": 1,
            "filter": ['id IN ["prod-1"]'],
            "attributesToRetrieve": DISPLAY_PROJECTION_FIELDS,
        },
    ]


def test_setec_catalog_client_stops_before_a_later_lookup_batch_when_shutdown_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    use_lookup_batch_size(monkeypatch, SMALL_LOOKUP_BATCH_SIZE)
    post = RecordingPost(
        [
            StubResponse(
                search_payload(
                    [
                        product_payload("prod-1", "product-1"),
                        product_payload("prod-2", "product-2"),
                    ],
                ),
            ),
            StubResponse(
                search_payload(
                    [
                        product_payload("prod-3", "product-3"),
                        product_payload("prod-4", "product-4"),
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        SetecCatalogClient().fetch_products_by_ids(
            CATALOG_URL,
            ["prod-1", "prod-2", "prod-3", "prod-4"],
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=lambda: len(post.bodies) >= 1,
        )

    # Then
    assert len(post.bodies) == 1
    assert requested_identifiers(post.bodies[0]) == ["prod-1", "prod-2"]


def test_setec_catalog_client_rejects_a_lookup_batch_returning_more_hits_than_it_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            StubResponse(
                search_payload(
                    [
                        product_payload("prod-1", "product-1"),
                        product_payload("prod-2", "product-2"),
                        product_payload("prod-3", "product-3"),
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="PageCardinalityExceeded") as raised:
        SetecCatalogClient().fetch_products_by_ids(
            CATALOG_URL,
            ["prod-1", "prod-2"],
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    assert raised.value.retryable is False
    assert len(post.bodies) == 1


def test_setec_catalog_client_rejects_a_looked_up_product_returned_with_two_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    use_lookup_batch_size(monkeypatch, SMALL_LOOKUP_BATCH_SIZE)
    post = RecordingPost(
        [
            StubResponse(
                search_payload(
                    [
                        product_payload("prod-1", "product-1", price=1_499),
                        product_payload("prod-2", "product-2"),
                    ],
                ),
            ),
            StubResponse(
                search_payload(
                    [
                        product_payload("prod-1", "product-1", price=999),
                        product_payload("prod-3", "product-3"),
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="DuplicateProductId") as raised:
        SetecCatalogClient().fetch_products_by_ids(
            CATALOG_URL,
            ["prod-1", "prod-2", "prod-3", "prod-4"],
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    # A lookup reads one snapshot of each identifier, so two values for one
    # product are a contradiction that retrying cannot resolve.
    assert raised.value.retryable is False
    assert len(post.bodies) == 2


def test_setec_catalog_client_collapses_a_looked_up_product_repeated_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    use_lookup_batch_size(monkeypatch, SMALL_LOOKUP_BATCH_SIZE)
    repeated = product_payload("prod-1", "product-1")
    post = RecordingPost(
        [
            StubResponse(
                search_payload([repeated, product_payload("prod-2", "product-2")]),
            ),
            StubResponse(
                search_payload([repeated, product_payload("prod-3", "product-3")]),
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When
    products = SetecCatalogClient().fetch_products_by_ids(
        CATALOG_URL,
        ["prod-1", "prod-2", "prod-3", "prod-4"],
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == ["prod-1", "prod-2", "prod-3"]
    assert len(post.bodies) == 2


@pytest.mark.parametrize(
    "malformed_hit",
    [
        hit_without_a_title("prod-2", "product-2"),
        hit_with_a_null_original_amount("prod-2", "product-2"),
    ],
    ids=["missing_title", "null_original_amount"],
)
def test_setec_catalog_client_skips_one_malformed_hit_without_losing_its_batch(
    malformed_hit: dict[str, JsonValue],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    # One unusable document must be treated exactly like an absent one, or a
    # single bad product would wedge every alert sharing its batch.
    post = RecordingPost(
        [
            StubResponse(
                search_payload(
                    [
                        product_payload("prod-1", "product-1"),
                        malformed_hit,
                        product_payload("prod-3", "product-3"),
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When
    products = SetecCatalogClient().fetch_products_by_ids(
        CATALOG_URL,
        ["prod-1", "prod-2", "prod-3"],
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == ["prod-1", "prod-3"]
    assert len(post.bodies) == 1
