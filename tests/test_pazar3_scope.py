from urllib.parse import parse_qsl, urlsplit

import pytest

from rss2discord.transports import FeedFetchError
from rss2discord.transports.pazar3_scope import Pazar3SearchScope

SEARCH_URL = (
    "https://www.pazar3.mk/oglasi/elektronika/"
    "delovi-za-kompjuteri-dodatoci/prodazba?Private=False"
)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.pazar3.mk/oglasi/elektronika",
        "https://example.test/oglasi/elektronika",
        "https://www.pazar3.mk:444/oglasi/elektronika",
        "https://user:secret@www.pazar3.mk/oglasi/elektronika",
        "https://www.pazar3.mk/oglas/elektronika/item/123",
        "https://www.pazar3.mk/oglasi/elektronika#results",
        "https://www.pazar3.mk/oglasi/../mk/Home2/Search",
        "https://www.pazar3.mk/oglasi/%2e%2e/mk/Home2/Search",
        "https://www.pazar3.mk/oglasi/elektronika%2f..%2fmk/Home2/Search",
        "https://www.pazar3.mk/oglasi/elektronika%5c..%5cmk/Home2/Search",
        "https://www.pazar3.mk/oglasi/elektronika%zz",
        "https://www.pazar3.mk/oglasi/elektronika\n/mk/Home2/Search",
    ],
)
def test_pazar3_scope_rejects_urls_outside_public_listing_boundary(url: str) -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        Pazar3SearchScope.from_url(url)

    assert fetch_error.value.cause_type == "InvalidUrl"
    assert "secret" not in str(fetch_error.value)


@pytest.mark.parametrize("host", ["pazar3.mk", "www.pazar3.mk"])
def test_pazar3_scope_accepts_listing_paths_and_preserves_filters(host: str) -> None:
    scope = Pazar3SearchScope.from_url(
        f"https://{host}/oglasi/elektronika/q-monitor?Private=False&Prop=1-2",
    )

    assert scope.scheme == "https"
    assert scope.host == host
    assert scope.port == 443
    assert scope.configured_path == "/oglasi/elektronika/q-monitor"
    assert scope.caller_query == (("Private", "False"), ("Prop", "1-2"))


def test_pazar3_page_request_replaces_page_and_preserves_filter_multimap() -> None:
    scope = Pazar3SearchScope.from_url(
        f"{SEARCH_URL}&tag=&tag=x&PAGE=8",
    )

    request = scope.page_request(2)

    assert parse_qsl(urlsplit(request.url).query, keep_blank_values=True) == [
        ("Private", "False"),
        ("tag", ""),
        ("tag", "x"),
        ("Page", "2"),
    ]


@pytest.mark.parametrize("page", [1, 2, 3])
def test_pazar3_page_request_accepts_recent_window(page: int) -> None:
    assert Pazar3SearchScope.from_url(SEARCH_URL).page_request(page).page == page


@pytest.mark.parametrize("page", [0, 4])
def test_pazar3_page_request_rejects_pages_outside_recent_window(page: int) -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        Pazar3SearchScope.from_url(SEARCH_URL).page_request(page)

    assert fetch_error.value.cause_type == "InvalidPage"


def test_pazar3_catalog_page_request_accepts_complete_scope_bound() -> None:
    request = Pazar3SearchScope.from_url(SEARCH_URL).catalog_page_request(10)

    assert request.page == 10
    assert parse_qsl(urlsplit(request.url).query)[-1] == ("Page", "10")


@pytest.mark.parametrize("page", [0, 11])
def test_pazar3_catalog_page_request_rejects_pages_outside_bound(page: int) -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        Pazar3SearchScope.from_url(SEARCH_URL).catalog_page_request(page)

    assert fetch_error.value.cause_type == "InvalidPage"


def test_pazar3_redirect_accepts_query_reordering() -> None:
    scope = Pazar3SearchScope.from_url(f"{SEARCH_URL}&tag=&tag=x")
    request = scope.page_request(2)

    assert scope.accepts_redirect(
        request,
        (
            "https://www.pazar3.mk/oglasi/elektronika/"
            "delovi-za-kompjuteri-dodatoci/prodazba?"
            "tag=x&Page=2&Private=False&tag="
        ),
    )


@pytest.mark.parametrize(
    "target_url",
    [
        "https://pazar3.mk/oglasi/elektronika/delovi-za-kompjuteri-dodatoci/prodazba?Private=False&Page=2",
        "https://www.pazar3.mk/oglasi/elektronika?Private=False&Page=2",
        "https://www.pazar3.mk/oglasi/elektronika/delovi-za-kompjuteri-dodatoci/prodazba?Private=True&Page=2",
        "https://www.pazar3.mk/oglasi/elektronika/delovi-za-kompjuteri-dodatoci/prodazba?Private=False&Page=2#results",
    ],
)
def test_pazar3_redirect_rejects_changed_scope(target_url: str) -> None:
    scope = Pazar3SearchScope.from_url(SEARCH_URL)

    assert not scope.accepts_redirect(scope.page_request(2), target_url)
