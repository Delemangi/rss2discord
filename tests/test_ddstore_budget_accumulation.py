import pytest

from rss2discord.transports import FeedFetchError, ddstore_catalog, ddstore_http
from rss2discord.transports.ddstore_catalog import DDStoreCatalogClient
from tests.ddstore_helpers import (
    CATALOG_URL,
    RecordingPost,
    StubResponse,
    catalog_payload,
    catalog_scan_should_stop,
    no_wait_fetch_retry_policy,
    product_payload,
)


def test_catalog_scan_accumulates_bytes_across_page_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_content = catalog_payload(
        501,
        [product_payload(str(index)) for index in range(1, 501)],
        current_page=1,
    )
    second_content = catalog_payload(
        501,
        [product_payload("501")],
        current_page=2,
    )
    post = RecordingPost(
        [StubResponse(first_content), StubResponse(second_content)],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)
    monkeypatch.setattr(
        ddstore_catalog,
        "MAX_DDSTORE_CATALOG_SCAN_BYTES",
        len(first_content) + len(second_content) - 1,
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        DDStoreCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    assert len(post.payloads) == 2
