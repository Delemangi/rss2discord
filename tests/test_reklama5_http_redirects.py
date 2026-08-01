from urllib.parse import urlsplit

import pytest
from curl_cffi import CurlECode
from curl_cffi import requests as curl_requests

from rss2discord.transports import FeedFetchError, reklama5_http
from rss2discord.transports.reklama5_http import fetch_reklama5_page
from tests.reklama5_helpers import RecordingGet, StubResponse, scan_budget, search_scope


def test_reklama5_fetch_sends_required_headers_and_stream_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([StubResponse(b"page")])
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    content = fetch_reklama5_page(search_scope().page_request(1), scan_budget())

    assert content == b"page"
    assert get.headers == [
        {
            "Accept": "text/html",
            "User-Agent": "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)",
        },
    ]
    assert get.allow_redirects == [False]
    assert get.stream == [True]


def test_reklama5_fetch_resolves_each_relative_redirect_against_response_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = search_scope().page_request(1)
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
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    content = fetch_reklama5_page(request, scan_budget())

    assert content == b"page"
    assert get.urls == [request.url, first_url, second_url]


def test_reklama5_fetch_shares_redirect_limit_across_page_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = search_scope()
    responses: list[StubResponse] = []
    for page in (1, 2):
        request = scope.page_request(page)
        responses.extend(
            StubResponse(
                b"redirect",
                status_code=302,
                headers={"Location": request.url},
                url=request.url,
            )
            for _ in range(6)
        )
        responses.append(StubResponse(b"page", url=request.url))
    get = RecordingGet(responses)
    budget = scan_budget()
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

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
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet(
            [StubResponse(b"redirect", status_code=302, headers=headers)],
        ),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(search_scope().page_request(1), scan_budget())

    assert fetch_error.value.cause_type == "InvalidRedirect"
    assert not fetch_error.value.retryable


@pytest.mark.parametrize(
    "error",
    [
        curl_requests.RequestsError("connection", CurlECode.COULDNT_CONNECT),
        curl_requests.RequestsError("timeout", CurlECode.OPERATION_TIMEDOUT),
    ],
)
def test_reklama5_fetch_marks_request_transport_failures_retryable(
    monkeypatch: pytest.MonkeyPatch,
    error: curl_requests.RequestsError,
) -> None:
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet([], interruption=error),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(search_scope().page_request(1), scan_budget())

    assert fetch_error.value.cause_type == type(error).__name__
    assert fetch_error.value.retryable


def test_reklama5_fetch_marks_pre_response_invalid_url_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet(
            [],
            interruption=curl_requests.RequestsError(
                "invalid",
                CurlECode.URL_MALFORMAT,
            ),
        ),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(search_scope().page_request(1), scan_budget())

    assert fetch_error.value.cause_type == "InvalidURL"
    assert not fetch_error.value.retryable


@pytest.mark.parametrize(
    "interruption",
    [
        curl_requests.RequestsError("connection interrupted", CurlECode.RECV_ERROR),
        curl_requests.RequestsError("stream timed out", CurlECode.OPERATION_TIMEDOUT),
        curl_requests.RequestsError("stream interrupted", CurlECode.PARTIAL_FILE),
    ],
)
def test_reklama5_fetch_marks_streamed_request_failures_retryable(
    monkeypatch: pytest.MonkeyPatch,
    interruption: curl_requests.RequestsError,
) -> None:
    budget = scan_budget(bytes_remaining=100)
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet(
            [StubResponse(b"", chunks=(b"partial",), interruption=interruption)],
        ),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(search_scope().page_request(1), budget)

    assert fetch_error.value.cause_type == type(interruption).__name__
    assert fetch_error.value.retryable
    assert budget.bytes_remaining == 100 - len(b"partial")


@pytest.mark.parametrize("retry_after", ["2.5", "Fri, 01 Aug 2099 00:00:00 GMT"])
def test_reklama5_fetch_propagates_exact_retry_after_result(
    monkeypatch: pytest.MonkeyPatch,
    retry_after: str,
) -> None:
    parsed_values: list[str | None] = []

    def parse_retry_after(value: str | None) -> float:
        parsed_values.append(value)
        return 73.25

    monkeypatch.setattr(reklama5_http, "parse_retry_after", parse_retry_after)
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet(
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
        fetch_reklama5_page(search_scope().page_request(1), scan_budget())

    assert parsed_values == [retry_after]
    assert fetch_error.value.retry_after == 73.25
