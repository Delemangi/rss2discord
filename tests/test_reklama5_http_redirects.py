from urllib.parse import urlsplit

import pytest
import requests

from rss2discord.transports import FeedFetchError, reklama5_http
from rss2discord.transports.reklama5_http import fetch_reklama5_page
from tests.reklama5_helpers import RecordingGet, StubResponse, scan_budget, search_scope


def test_reklama5_fetch_sends_required_headers_and_stream_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([StubResponse(b"page")])
    monkeypatch.setattr(requests, "get", get)

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
    monkeypatch.setattr(requests, "get", get)

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
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(b"redirect", status_code=302, headers=headers)]),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(search_scope().page_request(1), scan_budget())

    assert fetch_error.value.cause_type == "InvalidRedirect"
    assert not fetch_error.value.retryable


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
        fetch_reklama5_page(search_scope().page_request(1), scan_budget())

    assert fetch_error.value.cause_type == type(error).__name__
    assert fetch_error.value.retryable


def test_reklama5_fetch_marks_pre_response_invalid_url_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_invalid_url(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise requests.exceptions.InvalidURL("invalid")

    monkeypatch.setattr(requests, "get", raise_invalid_url)

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(search_scope().page_request(1), scan_budget())

    assert fetch_error.value.cause_type == "InvalidURL"
    assert not fetch_error.value.retryable


@pytest.mark.parametrize(
    "interruption",
    [
        requests.ConnectionError("connection interrupted"),
        requests.Timeout("stream timed out"),
        requests.RequestException("stream interrupted"),
    ],
)
def test_reklama5_fetch_marks_streamed_request_failures_retryable(
    monkeypatch: pytest.MonkeyPatch,
    interruption: requests.RequestException,
) -> None:
    budget = scan_budget(bytes_remaining=100)
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet(
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
        fetch_reklama5_page(search_scope().page_request(1), scan_budget())

    assert parsed_values == [retry_after]
    assert fetch_error.value.retry_after == 73.25
