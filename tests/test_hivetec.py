from urllib.parse import parse_qs, urlsplit

import pytest

from rss2discord.models import SourceMetric
from rss2discord.transports import FeedFetchError, hivetec_budget, hivetec_transport
from rss2discord.transports.hivetec import HivetecStrategy
from tests.hivetec_helpers import (
    SHOP_URL,
    RecordingGet,
    StubResponse,
    dates_payload,
    product_payload,
    products_payload,
)


def test_hivetec_strategy_fetches_latest_products_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                products_payload([product_payload(3), product_payload(2)]),
                headers={"X-WP-Total": "2", "X-WP-TotalPages": "1"},
            ),
            StubResponse(dates_payload([3, 2])),
        ],
    )
    monkeypatch.setattr(hivetec_transport, "_perform_request", get)
    strategy = HivetecStrategy()

    # When
    entries, source_title = strategy.fetch_entries(SHOP_URL)
    data = strategy.get_entry_data(entries[0])

    # Then
    assert source_title == "Hivetec"
    assert strategy.seed_existing_on_first_fetch
    assert strategy.require_entries_for_initialization
    assert strategy.max_new_entries_per_fetch == 30
    assert strategy.max_delivery_history == 10_000
    assert [strategy.get_entry_id(entry) for entry in entries] == ["2", "3"]
    assert data.title == "Product 2 – Gaming"
    assert data.link == "https://hivetec.mk/product/product-2/"
    assert data.timestamp == "2026-08-04T02:00:00+00:00"
    assert data.image_url == (
        "https://hivetec.mk/wp-content/uploads/product-2-600x600.jpg"
    )
    assert data.categories == ("Computers",)
    assert data.source_metrics == (
        SourceMetric("Price", "1.499 ден."),
        SourceMetric("Original", "1.999 ден."),
        SourceMetric("Stock", "In stock"),
        SourceMetric("SKU", "SKU-2"),
    )
    assert [urlsplit(url).path for url in get.urls] == [
        "/wp-json/wc/store/v1/products",
        "/wp-json/wp/v2/product",
    ]
    assert parse_qs(urlsplit(get.urls[0]).query)["per_page"] == ["30"]
    assert parse_qs(urlsplit(get.urls[0]).query)["orderby"] == ["date"]


def test_hivetec_strategy_rejects_discovery_endpoint_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(
        hivetec_transport,
        "_perform_request",
        RecordingGet(
            [
                StubResponse(
                    products_payload([product_payload(3), product_payload(2)]),
                    headers={"X-WP-Total": "2", "X-WP-TotalPages": "1"},
                ),
                StubResponse(dates_payload([3, 1])),
            ],
        ),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="DiscoveryDrift"):
        HivetecStrategy().fetch_entries(SHOP_URL)


@pytest.mark.parametrize(
    "url",
    [
        "http://hivetec.mk/shop/",
        "https://www.hivetec.mk/shop/",
        "https://hivetec.mk/",
        "https://hivetec.mk/shop",
        "https://hivetec.mk:443/shop/",
        "https://hivetec.mk/shop/?orderby=date",
        "https://user:secret@hivetec.mk/shop/",
    ],
)
def test_hivetec_strategy_rejects_urls_outside_the_exact_https_shop(url: str) -> None:
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        HivetecStrategy().fetch_entries(url)


def test_hivetec_strategy_rejects_more_than_the_latest_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product_ids = list(range(31, 0, -1))
    monkeypatch.setattr(
        hivetec_transport,
        "_perform_request",
        RecordingGet(
            [
                StubResponse(
                    products_payload(
                        [product_payload(product_id) for product_id in product_ids],
                    ),
                    headers={"X-WP-Total": "31", "X-WP-TotalPages": "2"},
                ),
                StubResponse(dates_payload(product_ids)),
            ],
        ),
    )

    with pytest.raises(FeedFetchError, match="LatestWindowExceeded"):
        HivetecStrategy().fetch_entries(SHOP_URL)


def test_hivetec_strategy_stops_a_slow_stream_at_the_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    payload = products_payload([product_payload(1)])

    def expire_operation() -> None:
        clock[0] = 301.0

    monkeypatch.setattr(hivetec_budget, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        hivetec_transport,
        "_perform_request",
        RecordingGet(
            [
                StubResponse(
                    payload,
                    headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"},
                    chunks=(payload,),
                    on_chunk=expire_operation,
                ),
                StubResponse(dates_payload([1])),
            ],
        ),
    )

    with pytest.raises(FeedFetchError, match="ScanTimeout"):
        HivetecStrategy().fetch_entries(SHOP_URL)


def test_hivetec_recording_get_invokes_hook_for_each_content_chunk() -> None:
    calls: list[None] = []
    get = RecordingGet(
        [
            StubResponse(
                b"",
                chunks=(b"first", b"second"),
                on_chunk=lambda: calls.append(None),
            ),
        ],
    )

    _ = get(
        SHOP_URL,
        timeout_ms=1,
        header_callback=len,
        content_callback=len,
    )

    assert len(calls) == 2


def test_hivetec_strategy_rejects_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(
                b"",
                status_code=302,
                headers={"Location": "http://127.0.0.1/internal"},
            ),
        ],
    )
    monkeypatch.setattr(hivetec_transport, "_perform_request", get)

    with pytest.raises(FeedFetchError, match="InvalidRedirect"):
        HivetecStrategy().fetch_entries(SHOP_URL)
    assert len(get.urls) == 1


def test_hivetec_strategy_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hivetec_transport,
        "_perform_request",
        RecordingGet(
            [
                StubResponse(
                    b"[]",
                    headers={
                        "Content-Length": "1048577",
                        "X-WP-Total": "0",
                        "X-WP-TotalPages": "0",
                    },
                ),
            ],
        ),
    )

    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        HivetecStrategy().fetch_entries(SHOP_URL)


def test_hivetec_strategy_rejects_malformed_pagination_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hivetec_transport,
        "_perform_request",
        RecordingGet(
            [
                StubResponse(
                    products_payload([product_payload(1)]),
                    headers={"X-WP-Total": "invalid", "X-WP-TotalPages": "1"},
                ),
            ],
        ),
    )

    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        HivetecStrategy().fetch_entries(SHOP_URL)
