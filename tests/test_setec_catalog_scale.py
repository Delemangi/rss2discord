from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from rss2discord.transports.setec_catalog import SetecCatalogClient
from tests.setec_helpers import (
    CATALOG_URL,
    RecordingGet,
    StubResponse,
    catalog_payload,
    catalog_scan_should_stop,
    no_wait_fetch_retry_policy,
    product_payload,
)

CATALOG_PRODUCT_COUNT = 13_284
FORMER_CATALOG_PAGE_SIZE = 100
FORMER_MAX_CATALOG_PAGES = 100
CATALOG_PAGE_SIZE = 250


def test_setec_catalog_client_traverses_a_production_scale_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    page_offsets = range(0, CATALOG_PRODUCT_COUNT, CATALOG_PAGE_SIZE)
    get = RecordingGet(
        [
            StubResponse(
                catalog_payload(
                    CATALOG_PRODUCT_COUNT,
                    [
                        product_payload(
                            f"prod-{product_number:05d}",
                            f"product-{product_number:05d}",
                        )
                        for product_number in range(
                            page_offset,
                            min(page_offset + CATALOG_PAGE_SIZE, CATALOG_PRODUCT_COUNT),
                        )
                    ],
                ),
            )
            for page_offset in page_offsets
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When
    products = SetecCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert CATALOG_PRODUCT_COUNT > FORMER_CATALOG_PAGE_SIZE * FORMER_MAX_CATALOG_PAGES
    assert len(products) == CATALOG_PRODUCT_COUNT
    assert products[0].id == "prod-00000"
    assert products[-1].id == "prod-13283"
    assert len(get.urls) == len(page_offsets)
    assert [parse_qs(urlsplit(url).query)["limit"][0] for url in get.urls] == [
        "250",
    ] * len(page_offsets)
    assert parse_qs(urlsplit(get.urls[-1]).query)["offset"] == ["13250"]
