import pytest
from pydantic import JsonValue

from rss2discord.transports import (
    FeedFetchError,
    hivetec_bounds,
    hivetec_catalog,
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


def test_hivetec_catalog_rejects_aggregate_product_metadata_amplification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_count = 13
    products_per_page = 100
    responses: list[StubResponse] = []
    for page in range(1, page_count + 1):
        page_products = []
        for index in range(products_per_page):
            product_id = (page - 1) * products_per_page + index + 1
            item = product_payload(product_id)
            image: dict[str, JsonValue] = {
                "src": f"https://hivetec.mk/wp-content/uploads/{product_id}.jpg",
                "thumbnail": (
                    f"https://hivetec.mk/wp-content/uploads/{product_id}-600x600.jpg"
                ),
            }
            item["images"] = [image] * 16
            page_products.append(item)
        responses.append(
            StubResponse(
                products_payload(page_products),
                headers={
                    "X-WP-Total": str(page_count * products_per_page),
                    "X-WP-TotalPages": str(page_count),
                },
            ),
        )
    monkeypatch.setattr(
        hivetec_transport,
        "_perform_request",
        RecordingGet(responses),
    )

    with pytest.raises(FeedFetchError, match="MetadataLimitExceeded"):
        HivetecCatalogClient().fetch_catalog(
            SHOP_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_hivetec_catalog_rejects_aggregate_category_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hivetec_catalog, "MAX_HIVETEC_CATALOG_CATEGORIES", 1)
    monkeypatch.setattr(
        hivetec_transport,
        "_perform_request",
        RecordingGet(
            [
                StubResponse(
                    products_payload([product_payload(2), product_payload(1)]),
                    headers={"X-WP-Total": "2", "X-WP-TotalPages": "1"},
                ),
            ],
        ),
    )

    with pytest.raises(FeedFetchError, match="MetadataLimitExceeded"):
        HivetecCatalogClient().fetch_catalog(
            SHOP_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_hivetec_catalog_rejects_scan_response_budget_overrun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = products_payload([product_payload(1)])
    monkeypatch.setattr(
        hivetec_bounds,
        "MAX_HIVETEC_CATALOG_SCAN_BYTES",
        len(payload) - 1,
    )
    monkeypatch.setattr(
        hivetec_transport,
        "_perform_request",
        RecordingGet(
            [
                StubResponse(
                    payload,
                    headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"},
                ),
            ],
        ),
    )

    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        HivetecCatalogClient().fetch_catalog(
            SHOP_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_hivetec_catalog_shares_response_bytes_across_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page = products_payload([product_payload(3), product_payload(2)])
    second_page = products_payload([product_payload(1)])
    headers = {"X-WP-Total": "3", "X-WP-TotalPages": "2"}
    monkeypatch.setattr(
        hivetec_bounds,
        "MAX_HIVETEC_CATALOG_SCAN_BYTES",
        len(first_page) + len(second_page),
    )
    monkeypatch.setattr(
        hivetec_transport,
        "_perform_request",
        RecordingGet(
            [
                StubResponse(first_page, headers=headers),
                StubResponse(b"failure", status_code=503),
                StubResponse(first_page, headers=headers),
                StubResponse(second_page, headers=headers),
            ],
        ),
    )

    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        HivetecCatalogClient().fetch_catalog(
            SHOP_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_hivetec_catalog_counts_response_headers_against_scan_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = products_payload([product_payload(1)])
    monkeypatch.setattr(
        hivetec_bounds,
        "MAX_HIVETEC_CATALOG_SCAN_BYTES",
        len(payload) + 128,
    )
    monkeypatch.setattr(
        hivetec_transport,
        "_perform_request",
        RecordingGet(
            [
                StubResponse(
                    payload,
                    headers={
                        "X-WP-Total": "1",
                        "X-WP-TotalPages": "1",
                        "X-Padding": "x" * 256,
                    },
                ),
            ],
        ),
    )

    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        HivetecCatalogClient().fetch_catalog(
            SHOP_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_hivetec_catalog_retries_overflowing_retry_after_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = products_payload([product_payload(1)])
    headers = {"X-WP-Total": "1", "X-WP-TotalPages": "1"}
    monkeypatch.setattr(
        hivetec_transport,
        "_perform_request",
        RecordingGet(
            [
                StubResponse(
                    b"failure",
                    status_code=503,
                    headers={
                        "Retry-After": "Sun, 06 Nov 9999999999 08:49:37 GMT",
                    },
                ),
                StubResponse(payload, headers=headers),
            ],
        ),
    )

    products = HivetecCatalogClient().fetch_catalog(
        SHOP_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    assert [product.id for product in products] == [1]
