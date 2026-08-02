from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from rss2discord.transports import FeedFetchError
from rss2discord.transports.reklama5 import parse_reklama5_page
from tests.reklama5_helpers import (
    FIXED_NOW,
    SEARCH_URL,
    Reklama5Card,
    page_request,
    replace_paginator_hrefs,
    search_page,
    search_scope,
)


def _page(
    *,
    cards: list[Reklama5Card],
    page_links: list[int],
    result_count: int,
) -> bytes:
    return search_page(
        1,
        [card.html() for card in cards],
        page_links=page_links,
        result_count=result_count,
        active_page=1 if page_links else None,
    )


def _assert_invalid_paginator(href: str) -> None:
    html = replace_paginator_hrefs(
        _page(cards=[Reklama5Card()], page_links=[1, 2], result_count=1),
        [href],
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        parse_reklama5_page(html, page_request(), FIXED_NOW)

    assert fetch_error.value.cause_type == "InvalidPaginator"
    assert not fetch_error.value.retryable


@pytest.mark.parametrize(
    "href",
    [
        "https://example.test/Search?cat=584&sell=1&buy=0&trade=0&includeOld=1&includeNew=1&SortByPrice=2&pageView=1&page=2",
        "http://reklama5.mk/Search?cat=584&sell=1&buy=0&trade=0&includeOld=1&includeNew=1&SortByPrice=2&pageView=1&page=2",
        "https://reklama5.mk:8443/Search?cat=584&sell=1&buy=0&trade=0&includeOld=1&includeNew=1&SortByPrice=2&pageView=1&page=2",
        "https://reklama5.mk/Other?cat=584&sell=1&buy=0&trade=0&includeOld=1&includeNew=1&SortByPrice=2&pageView=1&page=2",
        "https://user:secret@reklama5.mk/Search?cat=584&sell=1&buy=0&trade=0&includeOld=1&includeNew=1&SortByPrice=2&pageView=1&page=2",
        "https://reklama5.mk/Search?cat=584&sell=1&buy=0&trade=0&includeOld=1&includeNew=1&SortByPrice=2&pageView=1&page=2#fragment",
        "https://reklama5.mk/Search?cat=999&sell=1&buy=0&trade=0&includeOld=1&includeNew=1&SortByPrice=2&pageView=1&page=2",
        "https://reklama5.mk/Search?cat=584&cat=584&sell=1&buy=0&trade=0&includeOld=1&includeNew=1&SortByPrice=2&pageView=1&page=2",
        "https://reklama5.mk/Search?cat=584&sell=1&buy=0&trade=0&includeOld=1&includeNew=1&page=2",
    ],
    ids=[
        "host",
        "scheme",
        "port",
        "path",
        "credentials",
        "fragment",
        "filter",
        "duplicate",
        "forced",
    ],
)
def test_reklama5_parser_rejects_untrusted_paginator_scope(href: str) -> None:
    _assert_invalid_paginator(href)


def test_reklama5_parser_rejects_filter_equivalent_empty_fragment_paginator() -> None:
    _assert_invalid_paginator(f"{search_scope().page_request(2).url}#")


@pytest.mark.parametrize(
    "page_values",
    [[], ["2", "3"], [""], ["0"], ["-1"], ["2.5"], ["2 arbitrary"]],
    ids=["missing", "duplicate", "empty", "zero", "negative", "non-decimal", "suffix"],
)
def test_reklama5_parser_requires_one_positive_decimal_paginator_page(
    page_values: list[str],
) -> None:
    request_url = search_scope().page_request(2).url
    parsed = urlsplit(request_url)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "page"]
    href = urlunsplit(
        (
            *parsed[:3],
            urlencode([*query, *(("page", value) for value in page_values)]),
            "",
        ),
    )

    _assert_invalid_paginator(href)


def test_reklama5_parser_rejects_oversized_decimal_paginator_page() -> None:
    request_url = search_scope().page_request(2).url
    parsed = urlsplit(request_url)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "page"]
    href = urlunsplit(
        (*parsed[:3], urlencode([*query, ("page", "9" * 5_000)]), ""),
    )

    _assert_invalid_paginator(href)


def test_reklama5_parser_accepts_reordered_filter_equivalent_paginator_links() -> None:
    request_url = search_scope().page_request(2).url
    parsed = urlsplit(request_url)
    reordered = urlunsplit(
        (*parsed[:3], urlencode(list(reversed(parse_qsl(parsed.query)))), ""),
    )
    html = replace_paginator_hrefs(
        _page(cards=[Reklama5Card()], page_links=[1, 2], result_count=1),
        [reordered],
    )

    page = parse_reklama5_page(html, page_request(), FIXED_NOW)

    assert page.terminal is False


def test_reklama5_parser_accepts_live_previous_page_link_suffix() -> None:
    request = search_scope().page_request(2)
    previous_page_href = (
        search_scope()
        .page_request(1)
        .url.replace(
            "page=1",
            "page=1%20prev-nextPage",
        )
    )
    html = replace_paginator_hrefs(
        search_page(
            2,
            [Reklama5Card().html()],
            page_links=[1],
            result_count=1,
            active_page=2,
        ),
        [previous_page_href],
    )

    page = parse_reklama5_page(html, request, FIXED_NOW)

    assert page.terminal is True


@pytest.mark.parametrize(
    ("cards", "page_links", "expected"),
    [([Reklama5Card()], [], True), ([Reklama5Card()], [1, 2], False)],
    ids=["without-greater-link", "with-greater-link"],
)
def test_reklama5_parser_marks_non_zero_page_terminal_only_with_ids_and_without_a_greater_link(
    cards: list[Reklama5Card],
    page_links: list[int],
    expected: bool,
) -> None:
    page = parse_reklama5_page(
        _page(cards=cards, page_links=page_links, result_count=1),
        page_request(SEARCH_URL),
        FIXED_NOW,
    )

    assert page.terminal is expected


def test_reklama5_parser_keeps_empty_non_zero_page_without_links_non_terminal() -> None:
    page = parse_reklama5_page(
        _page(cards=[], page_links=[], result_count=1),
        page_request(),
        FIXED_NOW,
    )

    assert page.terminal is False


def test_reklama5_parser_marks_duplicate_id_non_zero_page_without_links_terminal() -> (
    None
):
    page = parse_reklama5_page(
        _page(
            cards=[Reklama5Card(ad_id="42"), Reklama5Card(ad_id="42")],
            page_links=[],
            result_count=2,
        ),
        page_request(),
        FIXED_NOW,
    )

    assert page.organic_ids == {"42"}
    assert page.terminal is True


def test_reklama5_parser_does_not_treat_row_count_as_terminal_evidence() -> None:
    page = parse_reklama5_page(
        _page(cards=[], page_links=[1, 2], result_count=1),
        page_request(),
        FIXED_NOW,
    )

    assert page.terminal is False
