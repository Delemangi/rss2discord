from urllib.parse import parse_qsl, urlsplit

import pytest

from rss2discord.transports import FeedFetchError
from rss2discord.transports.reklama5_http import Reklama5SearchScope
from tests.reklama5_helpers import SEARCH_URL


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
        "https://www.reklama5.mk/Search?cat=584&SortByPrice=2&pageView=1&page=1",
    )

    assert not accepted


def test_reklama5_redirect_rejects_explicit_port_zero() -> None:
    scope = Reklama5SearchScope.from_url("https://reklama5.mk/Search?cat=584")
    request = scope.page_request(1)

    assert not scope.accepts_redirect(
        request,
        "https://reklama5.mk:0/Search?cat=584&SortByPrice=2&pageView=1&page=1",
    )


def test_reklama5_redirect_rejects_explicit_empty_fragment() -> None:
    scope = Reklama5SearchScope.from_url("https://reklama5.mk/Search?cat=584")
    request = scope.page_request(1)

    assert not scope.accepts_redirect(
        request,
        "https://reklama5.mk/Search?cat=584&SortByPrice=2&pageView=1&page=1#",
    )


@pytest.mark.parametrize(
    "target_url",
    [
        "https://reklama5.mk/Search?cat=584&tag=&tag=x&extra=1&SortByPrice=2&pageView=1&page=2",
        "https://reklama5.mk/Search?cat=584&tag=&SortByPrice=2&pageView=1&page=2",
        "https://reklama5.mk/Search?cat=585&tag=&tag=x&SortByPrice=2&pageView=1&page=2",
        "https://reklama5.mk/Search?cat=584&cat=584&tag=&tag=x&SortByPrice=2&pageView=1&page=2",
        "https://reklama5.mk/Search?cat=584&tag=y&tag=x&SortByPrice=2&pageView=1&page=2",
    ],
)
def test_reklama5_redirect_rejects_changed_filter_multimap(target_url: str) -> None:
    scope = Reklama5SearchScope.from_url(
        "https://reklama5.mk/Search?cat=584&tag=&tag=x",
    )

    assert not scope.accepts_redirect(scope.page_request(2), target_url)


@pytest.mark.parametrize(
    "target_url",
    [
        "/Search?cat=584&SortByPrice=2&pageView=1&page=1",
        "https://reklama5.mk:invalid/Search?cat=584&SortByPrice=2&pageView=1&page=1",
        "https://reklama5.mk/Other?cat=584&SortByPrice=2&pageView=1&page=1",
        "https://reklama5.mk/Search?cat=584&SortByPrice=2&pageView=1&page=1#results",
    ],
)
def test_reklama5_redirect_rejects_untrusted_absolute_targets(
    target_url: str,
) -> None:
    scope = Reklama5SearchScope.from_url("https://reklama5.mk/Search?cat=584")

    assert not scope.accepts_redirect(scope.page_request(1), target_url)
