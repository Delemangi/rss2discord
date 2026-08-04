from __future__ import annotations

from datetime import datetime

import pytest

from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports import FeedFetchError, reklama5_catalog, reklama5_http
from rss2discord.transports.reklama5_catalog import Reklama5CatalogClient
from tests.reklama5_helpers import (
    FIXED_NOW,
    SEARCH_URL,
    RecordingGet,
    Reklama5Card,
    StubResponse,
    replace_paginator_hrefs,
    requested_pages,
    search_page,
    search_scope,
)


def _response(
    page: int,
    ad_id: str,
    *,
    terminal: bool,
    result_count: int = 1,
) -> StubResponse:
    html = search_page(
        page,
        [Reklama5Card(ad_id=ad_id).html()],
        page_links=[] if terminal else [1],
        result_count=result_count,
        active_page=None if terminal else page,
    )
    if not terminal:
        scope = search_scope()
        html = replace_paginator_hrefs(
            html,
            [scope.catalog_page_request(page + 1).url],
        )
    return StubResponse(html)


def _retry_policy() -> FetchRetryPolicy:
    return FetchRetryPolicy(
        sleep=lambda seconds: True,
        on_retry=lambda error, delay: None,
    )


def _clock() -> datetime:
    return FIXED_NOW


def test_reklama5_catalog_fetches_beyond_the_discovery_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = RecordingGet(
        [
            _response(1, "4", terminal=False, result_count=4),
            _response(2, "3", terminal=False, result_count=4),
            _response(3, "2", terminal=False, result_count=4),
            _response(4, "1", terminal=True, result_count=4),
        ],
    )
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: session)
    client = Reklama5CatalogClient(clock=_clock)

    listings = client.fetch_catalog(
        SEARCH_URL,
        retry_policy=_retry_policy(),
        is_shutdown_requested=lambda: False,
    )

    assert requested_pages(session.urls) == ["1", "2", "3", "4"]
    assert [listing.entry_id for listing in listings] == ["4", "3", "2", "1"]


def test_reklama5_catalog_attempt_budget_covers_the_approved_category_bound() -> None:
    budget = reklama5_http.Reklama5ScanBudget.for_catalog()

    assert budget.bytes_remaining == 500 * 1024 * 1024


def test_reklama5_catalog_rejects_listing_capacity_before_returning_partial_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = search_page(
        1,
        [Reklama5Card(ad_id="1").html(), Reklama5Card(ad_id="2").html()],
        page_links=[],
        result_count=2,
    )
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet([StubResponse(html)]),
    )
    monkeypatch.setattr(reklama5_catalog, "MAX_REKLAMA5_CATALOG_LISTINGS", 1)

    with pytest.raises(FeedFetchError) as fetch_error:
        Reklama5CatalogClient(clock=_clock).fetch_catalog(
            SEARCH_URL,
            retry_policy=_retry_policy(),
            is_shutdown_requested=lambda: False,
        )

    assert fetch_error.value.cause_type == "ProductLimitExceeded"


def test_reklama5_catalog_rejects_terminal_page_before_advertised_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = search_page(
        1,
        [Reklama5Card(ad_id="1").html()],
        page_links=[],
        result_count=2,
    )
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet(
            [StubResponse(html), StubResponse(html), StubResponse(html)],
        ),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        Reklama5CatalogClient(clock=_clock).fetch_catalog(
            SEARCH_URL,
            retry_policy=_retry_policy(),
            is_shutdown_requested=lambda: False,
        )

    assert fetch_error.value.cause_type == "IncompleteCatalog"
    assert fetch_error.value.retryable


@pytest.mark.parametrize(
    "malformed_card",
    [
        pytest.param(Reklama5Card(ad_id="2", title=""), id="empty-title"),
        pytest.param(
            Reklama5Card(
                ad_id="2",
                href="https://example.com/AdDetails?ad=2",
            ),
            id="cross-origin-url",
        ),
        pytest.param(
            Reklama5Card(ad_id="2", timestamp="not-a-date"),
            id="invalid-timestamp",
        ),
    ],
)
def test_reklama5_catalog_rejects_malformed_rows_before_returning_partial_data(
    monkeypatch: pytest.MonkeyPatch,
    malformed_card: Reklama5Card,
) -> None:
    html = search_page(
        1,
        [Reklama5Card(ad_id="1").html(), malformed_card.html()],
        page_links=[],
        result_count=2,
    )
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet([StubResponse(html) for _attempt in range(3)]),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        Reklama5CatalogClient(clock=_clock).fetch_catalog(
            SEARCH_URL,
            retry_policy=_retry_policy(),
            is_shutdown_requested=lambda: False,
        )

    assert fetch_error.value.cause_type == "IncompleteCatalog"
    assert fetch_error.value.retryable


def test_reklama5_catalog_rejects_changed_advertised_result_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_html = replace_paginator_hrefs(
        search_page(
            1,
            [Reklama5Card(ad_id="2").html()],
            page_links=[1],
            result_count=2,
            active_page=1,
        ),
        [search_scope().catalog_page_request(2).url],
    )
    second_html = search_page(
        2,
        [Reklama5Card(ad_id="1").html()],
        page_links=[],
        result_count=1,
    )
    responses = [
        response
        for _attempt in range(3)
        for response in (StubResponse(first_html), StubResponse(second_html))
    ]
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet(responses),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        Reklama5CatalogClient(clock=_clock).fetch_catalog(
            SEARCH_URL,
            retry_policy=_retry_policy(),
            is_shutdown_requested=lambda: False,
        )

    assert fetch_error.value.cause_type == "ChangedResultCount"
    assert fetch_error.value.retryable


def test_reklama5_catalog_counts_duplicate_organic_rows_toward_completeness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = search_page(
        1,
        [Reklama5Card(ad_id="1").html(), Reklama5Card(ad_id="1").html()],
        page_links=[],
        result_count=2,
    )
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet([StubResponse(html)]),
    )

    listings = Reklama5CatalogClient(clock=_clock).fetch_catalog(
        SEARCH_URL,
        retry_policy=_retry_policy(),
        is_shutdown_requested=lambda: False,
    )

    assert [listing.entry_id for listing in listings] == ["1"]


def test_reklama5_catalog_fails_when_page_bound_has_no_terminal_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet([_response(1, "1", terminal=False)]),
    )
    monkeypatch.setattr(reklama5_catalog, "MAX_REKLAMA5_CATALOG_PAGES", 1)

    with pytest.raises(FeedFetchError) as fetch_error:
        Reklama5CatalogClient(clock=_clock).fetch_catalog(
            SEARCH_URL,
            retry_policy=_retry_policy(),
            is_shutdown_requested=lambda: False,
        )

    assert fetch_error.value.cause_type == "PageLimitExceeded"
