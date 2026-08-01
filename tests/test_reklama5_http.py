import math
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests

from rss2discord.transports import FeedFetchError, reklama5_http
from rss2discord.transports.reklama5_http import (
    MAX_REKLAMA5_ATTEMPT_BYTES,
    MAX_REKLAMA5_RESPONSE_BYTES,
    Reklama5ScanBudget,
    Reklama5SearchScope,
    fetch_reklama5_page,
)
from tests.reklama5_helpers import SEARCH_URL, RecordingGet, StubResponse


@pytest.mark.parametrize(
    "url",
    [
        "http://reklama5.mk/Search?cat=584",
        "https://example.test/Search?cat=584",
        "https://reklama5.mk:444/Search?cat=584",
        "https://user:secret@reklama5.mk/Search?cat=584",
        "https://reklama5.mk/Other?cat=584",
        "https://reklama5.mk/Search?cat=584#results",
    ],
)
def test_reklama5_scope_rejects_urls_outside_the_search_boundary(url: str) -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        Reklama5SearchScope.from_url(url)

    assert fetch_error.value.cause_type == "InvalidUrl"
    assert "secret" not in str(fetch_error.value)


def test_reklama5_scope_rejects_explicit_port_zero() -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        Reklama5SearchScope.from_url("https://reklama5.mk:0/Search?cat=584")

    assert fetch_error.value.cause_type == "InvalidUrl"


def test_reklama5_scope_rejects_explicit_empty_fragment() -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        Reklama5SearchScope.from_url("https://reklama5.mk/Search?cat=584#")

    assert fetch_error.value.cause_type == "InvalidUrl"


@pytest.mark.parametrize(
    ("url", "host", "path"),
    [
        ("https://reklama5.mk/Search?cat=584", "reklama5.mk", "/Search"),
        (
            "https://www.reklama5.mk:443/Search/?cat=584",
            "www.reklama5.mk",
            "/Search/",
        ),
        (
            "https://reklama5.mk:443/Search/Index?cat=584",
            "reklama5.mk",
            "/Search/Index",
        ),
        (
            "https://www.reklama5.mk/Search/Index/?cat=584",
            "www.reklama5.mk",
            "/Search/Index/",
        ),
    ],
)
def test_reklama5_scope_accepts_approved_origins_and_paths(
    url: str,
    host: str,
    path: str,
) -> None:
    scope = Reklama5SearchScope.from_url(url)

    assert scope.scheme == "https"
    assert scope.host == host
    assert scope.port == 443
    assert scope.configured_path == path
    assert scope.caller_query == (("cat", "584"),)


def test_reklama5_page_request_preserves_filters_and_replaces_owned_keys() -> None:
    scope = Reklama5SearchScope.from_url(
        "https://www.reklama5.mk/Search/?"
        "cat=584&tag=&tag=x&sortbyprice=9&PAGE=8&pageView=7",
    )

    request = scope.page_request(2)

    assert parse_qsl(urlsplit(request.url).query, keep_blank_values=True) == [
        ("cat", "584"),
        ("tag", ""),
        ("tag", "x"),
        ("SortByPrice", "2"),
        ("pageView", "1"),
        ("page", "2"),
    ]


@pytest.mark.parametrize("page", [1, 2, 3])
def test_reklama5_page_request_accepts_the_fixed_recent_window(page: int) -> None:
    scope = Reklama5SearchScope.from_url(SEARCH_URL)

    request = scope.page_request(page)

    assert request.scope == scope
    assert request.page == page
    assert parse_qsl(urlsplit(request.url).query, keep_blank_values=True)[-3:] == [
        ("SortByPrice", "2"),
        ("pageView", "1"),
        ("page", str(page)),
    ]


@pytest.mark.parametrize("page", [0, 4])
def test_reklama5_page_request_rejects_pages_outside_the_fixed_window(
    page: int,
) -> None:
    scope = Reklama5SearchScope.from_url(SEARCH_URL)

    with pytest.raises(FeedFetchError) as fetch_error:
        scope.page_request(page)

    assert fetch_error.value.cause_type == "InvalidPage"


def test_reklama5_redirect_accepts_search_path_and_query_reordering() -> None:
    scope = Reklama5SearchScope.from_url(
        "https://reklama5.mk/Search?cat=584&tag=&tag=x",
    )
    request = scope.page_request(2)

    accepted = scope.accepts_redirect(
        request,
        "https://reklama5.mk/Search/Index/?"
        "PAGE=2&tag=x&pageview=1&cat=584&SORTBYPRICE=2&tag=",
    )

    assert accepted


def test_reklama5_redirect_rejects_apex_to_www_switch() -> None:
    scope = Reklama5SearchScope.from_url("https://reklama5.mk/Search?cat=584")
    request = scope.page_request(1)

    accepted = scope.accepts_redirect(
        request,
        "https://www.reklama5.mk/Search?"
        "cat=584&SortByPrice=2&pageView=1&page=1",
    )

    assert not accepted


def test_reklama5_redirect_rejects_explicit_port_zero() -> None:
    scope = Reklama5SearchScope.from_url("https://reklama5.mk/Search?cat=584")
    request = scope.page_request(1)

    assert not scope.accepts_redirect(
        request,
        "https://reklama5.mk:0/Search?"
        "cat=584&SortByPrice=2&pageView=1&page=1",
    )


def test_reklama5_redirect_rejects_explicit_empty_fragment() -> None:
    scope = Reklama5SearchScope.from_url("https://reklama5.mk/Search?cat=584")
    request = scope.page_request(1)

    assert not scope.accepts_redirect(
        request,
        "https://reklama5.mk/Search?"
        "cat=584&SortByPrice=2&pageView=1&page=1#",
    )


@pytest.mark.parametrize(
    "target_url",
    [
        (
            "https://reklama5.mk/Search?"
            "cat=584&tag=&tag=x&extra=1&SortByPrice=2&pageView=1&page=2"
        ),
        (
            "https://reklama5.mk/Search?"
            "cat=584&tag=&SortByPrice=2&pageView=1&page=2"
        ),
        (
            "https://reklama5.mk/Search?"
            "cat=585&tag=&tag=x&SortByPrice=2&pageView=1&page=2"
        ),
        (
            "https://reklama5.mk/Search?"
            "cat=584&cat=584&tag=&tag=x&SortByPrice=2&pageView=1&page=2"
        ),
        (
            "https://reklama5.mk/Search?"
            "cat=584&tag=y&tag=x&SortByPrice=2&pageView=1&page=2"
        ),
    ],
)
def test_reklama5_redirect_rejects_changed_filter_multimap(
    target_url: str,
) -> None:
    scope = Reklama5SearchScope.from_url(
        "https://reklama5.mk/Search?cat=584&tag=&tag=x",
    )
    request = scope.page_request(2)

    assert not scope.accepts_redirect(request, target_url)


@pytest.mark.parametrize(
    "target_url",
    [
        "/Search?cat=584&SortByPrice=2&pageView=1&page=1",
        (
            "https://reklama5.mk:invalid/Search?"
            "cat=584&SortByPrice=2&pageView=1&page=1"
        ),
        "https://reklama5.mk/Other?cat=584&SortByPrice=2&pageView=1&page=1",
        (
            "https://reklama5.mk/Search?"
            "cat=584&SortByPrice=2&pageView=1&page=1#results"
        ),
    ],
)
def test_reklama5_redirect_rejects_untrusted_absolute_targets(
    target_url: str,
) -> None:
    scope = Reklama5SearchScope.from_url("https://reklama5.mk/Search?cat=584")
    request = scope.page_request(1)

    assert not scope.accepts_redirect(request, target_url)


def _scope() -> Reklama5SearchScope:
    return Reklama5SearchScope.from_url(SEARCH_URL)


def _budget(
    *,
    bytes_remaining: int = MAX_REKLAMA5_ATTEMPT_BYTES,
    redirects_remaining: int = 10,
    expires_at: float = math.inf,
) -> Reklama5ScanBudget:
    return Reklama5ScanBudget(bytes_remaining, redirects_remaining, expires_at)


def test_reklama5_fetch_uses_remaining_deadline_as_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([StubResponse(b"page")])
    budget = _budget(bytes_remaining=100, expires_at=15)
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(reklama5_http.time, "monotonic", lambda: 10)

    fetch_reklama5_page(_scope().page_request(1), budget)

    assert get.timeouts == [5]


def test_reklama5_fetch_disables_redirects_and_streams_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([StubResponse(b"page")])
    monkeypatch.setattr(requests, "get", get)

    content = fetch_reklama5_page(_scope().page_request(1), _budget())

    assert content == b"page"
    assert get.allow_redirects == [False]
    assert get.stream == [True]


def test_reklama5_fetch_resolves_each_relative_redirect_against_response_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()
    request = scope.page_request(1)
    first_url = request.url.replace("/Search?", "/Search/Index?")
    second_url = request.url.replace("/Search?", "/Search/?")
    get = RecordingGet(
        [
            StubResponse(
                b"first redirect",
                status_code=302,
                headers={"Location": f"Search/Index?{urlsplit(request.url).query}"},
                url=request.url,
            ),
            StubResponse(
                b"second redirect",
                status_code=307,
                headers={"Location": f"../Search/?{urlsplit(request.url).query}"},
                url=first_url,
            ),
            StubResponse(b"page", url=second_url),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    content = fetch_reklama5_page(request, _budget())

    assert content == b"page"
    assert get.urls == [request.url, first_url, second_url]


def test_reklama5_fetch_shares_redirect_limit_across_page_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()
    responses: list[StubResponse] = []
    for page in (1, 2):
        request = scope.page_request(page)
        responses.extend(
            [
                StubResponse(
                    b"redirect",
                    status_code=302,
                    headers={"Location": request.url},
                    url=request.url,
                )
                for _ in range(6)
            ],
        )
        responses.append(StubResponse(b"page", url=request.url))
    get = RecordingGet(responses)
    budget = _budget()
    monkeypatch.setattr(requests, "get", get)

    assert fetch_reklama5_page(scope.page_request(1), budget) == b"page"
    with pytest.raises(FeedFetchError, match="TooManyRedirects"):
        fetch_reklama5_page(scope.page_request(2), budget)

    assert len(get.urls) == 12


@pytest.mark.parametrize(
    "location",
    [
        None,
        "https://www.reklama5.mk/Search?cat=584&SortByPrice=2&pageView=1&page=1",
        "https://reklama5.mk/Other?cat=584&SortByPrice=2&pageView=1&page=1",
        "https://reklama5.mk/Search?cat=585&SortByPrice=2&pageView=1&page=1",
        "https://reklama5.mk:0/Search?cat=584&SortByPrice=2&pageView=1&page=1",
        "https://reklama5.mk/Search?cat=584&SortByPrice=2&pageView=1&page=1#",
    ],
)
def test_reklama5_fetch_rejects_missing_and_untrusted_redirects(
    monkeypatch: pytest.MonkeyPatch,
    location: str | None,
) -> None:
    headers = {} if location is None else {"Location": location}
    get = RecordingGet([StubResponse(b"redirect", status_code=302, headers=headers)])
    monkeypatch.setattr(requests, "get", get)

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(_scope().page_request(1), _budget())

    assert fetch_error.value.cause_type == "InvalidRedirect"
    assert not fetch_error.value.retryable


@pytest.mark.parametrize(
    "response",
    [
        StubResponse(
            b"",
            headers={"Content-Length": str(MAX_REKLAMA5_RESPONSE_BYTES + 1)},
        ),
        StubResponse(
            b"",
            chunks=(b"x" * MAX_REKLAMA5_RESPONSE_BYTES, b"y"),
        ),
        StubResponse(
            b"",
            status_code=302,
            headers={"Location": _scope().page_request(1).url},
            url=_scope().page_request(1).url,
            chunks=(b"x" * MAX_REKLAMA5_RESPONSE_BYTES, b"y"),
        ),
    ],
)
def test_reklama5_fetch_rejects_declared_and_streamed_oversized_bodies(
    monkeypatch: pytest.MonkeyPatch,
    response: StubResponse,
) -> None:
    monkeypatch.setattr(requests, "get", RecordingGet([response]))

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(_scope().page_request(1), _budget())

    assert fetch_error.value.cause_type == "ResponseTooLarge"
    assert not fetch_error.value.retryable


def test_reklama5_fetch_charges_redirect_and_final_bodies_to_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scope().page_request(1)
    get = RecordingGet(
        [
            StubResponse(
                b"redirect",
                status_code=302,
                headers={"Location": request.url},
                url=request.url,
            ),
            StubResponse(b"final", url=request.url),
        ],
    )
    budget = _budget(bytes_remaining=13)
    monkeypatch.setattr(requests, "get", get)

    content = fetch_reklama5_page(request, budget)

    assert content == b"final"
    assert budget.bytes_remaining == 13 - len(b"redirect") - len(b"final")


@pytest.mark.parametrize(
    "responses",
    [
        [
            StubResponse(
                b"redirect",
                status_code=302,
                headers={"Location": _scope().page_request(1).url},
                url=_scope().page_request(1).url,
            ),
            StubResponse(b"final"),
        ],
        [StubResponse(b"final")],
    ],
)
def test_reklama5_fetch_rejects_redirect_or_final_body_past_attempt_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[StubResponse],
) -> None:
    monkeypatch.setattr(requests, "get", RecordingGet(responses))

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(_scope().page_request(1), _budget(bytes_remaining=4))

    assert fetch_error.value.cause_type == "ScanByteLimitExceeded"
    assert not fetch_error.value.retryable


def test_reklama5_fetch_reads_and_charges_http_error_before_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _budget(bytes_remaining=100)
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(b"unavailable", status_code=503)]),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(_scope().page_request(1), budget)

    assert fetch_error.value.cause_type == "HTTPError"
    assert fetch_error.value.retryable
    assert budget.bytes_remaining == 100 - len(b"unavailable")


def test_reklama5_fetch_prioritizes_oversized_http_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = StubResponse(
        b"",
        status_code=503,
        chunks=(b"x" * MAX_REKLAMA5_RESPONSE_BYTES, b"y"),
    )
    monkeypatch.setattr(requests, "get", RecordingGet([response]))

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(_scope().page_request(1), _budget())

    assert fetch_error.value.cause_type == "ResponseTooLarge"
    assert not fetch_error.value.retryable


@pytest.mark.parametrize("status_code", range(400, 600))
def test_reklama5_fetch_classifies_http_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(b"error", status_code=status_code)]),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(_scope().page_request(1), _budget())

    assert fetch_error.value.status_code == status_code
    assert fetch_error.value.retryable is (
        status_code in {408, 429} or 500 <= status_code < 600
    )


def test_reklama5_fetch_rejects_empty_response_completed_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([StubResponse(b"", chunks=())])
    monotonic_values = iter((0.0, 2.0))
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(
        reklama5_http.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(
            _scope().page_request(1),
            _budget(bytes_remaining=100, expires_at=1),
        )

    assert fetch_error.value.cause_type == "ScanTimeLimitExceeded"
    assert not fetch_error.value.retryable


@pytest.mark.parametrize("remaining_seconds", [31.0, 0.25])
def test_reklama5_fetch_uses_positive_capped_request_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    remaining_seconds: float,
) -> None:
    get = RecordingGet([StubResponse(b"page")])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(reklama5_http.time, "monotonic", lambda: 10.0)

    fetch_reklama5_page(
        _scope().page_request(1),
        _budget(expires_at=10.0 + remaining_seconds),
    )

    assert get.timeouts == [min(30.0, remaining_seconds)]
    assert get.timeouts[0] > 0


@pytest.mark.parametrize(
    "error",
    [requests.ConnectionError("connection"), requests.Timeout("timeout")],
)
def test_reklama5_fetch_marks_request_transport_failures_retryable(
    monkeypatch: pytest.MonkeyPatch,
    error: requests.RequestException,
) -> None:
    def raise_request(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise error

    monkeypatch.setattr(requests, "get", raise_request)

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(_scope().page_request(1), _budget())

    assert fetch_error.value.cause_type == type(error).__name__
    assert fetch_error.value.retryable


def test_reklama5_fetch_marks_streamed_request_failures_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = StubResponse(
        b"",
        chunks=(b"partial",),
        interruption=requests.RequestException("stream interrupted"),
    )
    budget = _budget(bytes_remaining=100)
    monkeypatch.setattr(requests, "get", RecordingGet([response]))

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(_scope().page_request(1), budget)

    assert fetch_error.value.cause_type == "RequestException"
    assert fetch_error.value.retryable
    assert budget.bytes_remaining == 100 - len(b"partial")


@pytest.mark.parametrize(
    "retry_after",
    ["2.5", format_datetime(datetime(2099, 8, 1, tzinfo=UTC), usegmt=True)],
)
def test_reklama5_fetch_propagates_retry_after_metadata(
    monkeypatch: pytest.MonkeyPatch,
    retry_after: str,
) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet(
            [
                StubResponse(
                    b"busy",
                    status_code=429,
                    headers={"Retry-After": retry_after},
                ),
            ],
        ),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(_scope().page_request(1), _budget())

    assert fetch_error.value.retry_after is not None
    assert fetch_error.value.retry_after > 0


def test_reklama5_scan_budget_for_attempt_uses_fixed_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reklama5_http.time, "monotonic", lambda: 10.0)

    budget = Reklama5ScanBudget.for_attempt()

    assert budget.bytes_remaining == MAX_REKLAMA5_ATTEMPT_BYTES
    assert budget.redirects_remaining == 10
    assert budget.expires_at == 130.0
