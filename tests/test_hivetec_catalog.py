from urllib.parse import parse_qs, urlsplit

import pytest

from rss2discord.transports import (
    FeedFetchError,
    hivetec_bounds,
    hivetec_transport,
)
from rss2discord.transports.hivetec_catalog import HivetecCatalogClient
from tests.hivetec_helpers import (
    SHOP_URL,
    RecordingGet,
    StubResponse,
    catalog_scan_should_stop,
    no_wait_fetch_retry_policy,
    product_payload,
    products_payload,
)


def test_hivetec_catalog_fetches_every_page_in_api_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    headers = {"X-WP-Total": "3", "X-WP-TotalPages": "2"}
    get = RecordingGet(
        [
            StubResponse(
                products_payload([product_payload(3), product_payload(2)]),
                headers=headers,
            ),
            StubResponse(products_payload([product_payload(1)]), headers=headers),
        ],
    )
    monkeypatch.setattr(hivetec_transport, "_perform_request", get)

    # When
    products = HivetecCatalogClient().fetch_catalog(
        SHOP_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == [3, 2, 1]
    assert [parse_qs(urlsplit(url).query)["page"] for url in get.urls] == [
        ["1"],
        ["2"],
    ]
    assert all(parse_qs(urlsplit(url).query)["per_page"] == ["100"] for url in get.urls)


def test_hivetec_catalog_rejects_changed_pagination_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    drifting_attempt = [
        StubResponse(
            products_payload([product_payload(2)]),
            headers={"X-WP-Total": "2", "X-WP-TotalPages": "2"},
        ),
        StubResponse(
            products_payload([product_payload(1)]),
            headers={"X-WP-Total": "3", "X-WP-TotalPages": "2"},
        ),
    ]
    get = RecordingGet(drifting_attempt * 3)
    monkeypatch.setattr(hivetec_transport, "_perform_request", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="PaginationDrift"):
        HivetecCatalogClient().fetch_catalog(
            SHOP_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_hivetec_catalog_rejects_product_limit_before_requesting_later_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                products_payload([product_payload(1)]),
                headers={
                    "X-WP-Total": str(hivetec_bounds.MAX_HIVETEC_CATALOG_PRODUCTS + 1),
                    "X-WP-TotalPages": "2",
                },
            ),
        ],
    )
    monkeypatch.setattr(hivetec_transport, "_perform_request", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="ProductLimitExceeded"):
        HivetecCatalogClient().fetch_catalog(
            SHOP_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )
    assert len(get.urls) == 1


def test_hivetec_catalog_rejects_identical_duplicate_product_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = product_payload(2)
    headers = {"X-WP-Total": "3", "X-WP-TotalPages": "2"}
    monkeypatch.setattr(
        hivetec_transport,
        "_perform_request",
        RecordingGet(
            [
                StubResponse(
                    products_payload([duplicate, product_payload(1)]),
                    headers=headers,
                ),
                StubResponse(products_payload([duplicate]), headers=headers),
            ],
        ),
    )

    with pytest.raises(FeedFetchError, match="DuplicateProductId"):
        HivetecCatalogClient().fetch_catalog(
            SHOP_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )
