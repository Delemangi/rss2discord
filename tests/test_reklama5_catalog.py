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


def _response(page: int, ad_id: str, *, terminal: bool) -> StubResponse:
    html = search_page(
        page,
        [Reklama5Card(ad_id=ad_id).html()],
        page_links=[] if terminal else [1],
        result_count=1,
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
            _response(1, "4", terminal=False),
            _response(2, "3", terminal=False),
            _response(3, "2", terminal=False),
            _response(4, "1", terminal=True),
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
