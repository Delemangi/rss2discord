import pytest
from curl_cffi import requests

from rss2discord.transports import FeedFetchError
from rss2discord.transports.neksio_catalog import NeksioCatalogClient
from tests.neksio_helpers import (
    CATALOG_URL,
    RecordingGet,
    RecordingPost,
    StubResponse,
    homepage_payload,
    page_payload,
    product_card,
)


def test_fetch_catalog_rejects_pagination_metadata_drift_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_page = [product_card(product_id) for product_id in range(100, 200)]
    second_page = [product_card(product_id) for product_id in range(200, 300)]
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost(
            [
                StubResponse(page_payload(1, 1, 2, 101, first_page)),
                StubResponse(page_payload(1, 2, 3, 201, second_page)),
                StubResponse(page_payload(1, 3, 3, 201, [product_card(300)])),
            ],
        ),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="PaginationMetadataDrift") as fetch_error:
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)
    assert fetch_error.value.retryable
