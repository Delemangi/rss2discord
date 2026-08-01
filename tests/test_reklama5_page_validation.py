import pytest
from bs4 import BeautifulSoup

from rss2discord.transports import FeedFetchError
from rss2discord.transports.reklama5 import parse_reklama5_page
from tests.reklama5_helpers import (
    FIXED_NOW,
    Reklama5Card,
    fixture_html,
    page_request,
    replace_active_markers,
    replace_form_page_inputs,
    search_page,
    search_scope,
)


def _valid_page() -> bytes:
    return search_page(
        1,
        [Reklama5Card().html()],
        page_links=[1, 2],
        result_count=1,
        active_page=1,
    )


def _without_selector(html: bytes, selector: str) -> bytes:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(selector)
    assert node is not None
    node.decompose()
    return soup.encode()


def _assert_page_error(html: bytes, cause_type: str = "InvalidPage") -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        parse_reklama5_page(html, page_request(), FIXED_NOW)

    assert fetch_error.value.cause_type == cause_type
    assert not fetch_error.value.retryable


def test_reklama5_parser_accepts_explicit_zero_result_page() -> None:
    page = parse_reklama5_page(fixture_html("zero_results.html"), page_request(), FIXED_NOW)

    assert page.listings == ()
    assert page.organic_ids == frozenset()
    assert page.terminal is True


def test_reklama5_parser_rejects_empty_html_without_zero_result_evidence() -> None:
    _assert_page_error(b"")


def test_reklama5_parser_rejects_exact_application_error_inside_valid_page() -> None:
    _assert_page_error(fixture_html("application_error_page.html"), "ApplicationError")


@pytest.mark.parametrize(
    "html",
    [
        b"<html><body>Checking your browser before accessing</body></html>",
        b'<html><body><form id="login"></form></body></html>',
        b"<html><body>Reklama5 homepage</body></html>",
        b"<html><body>unrelated document</body></html>",
    ],
    ids=["challenge", "login", "homepage", "unrelated"],
)
def test_reklama5_parser_rejects_challenge_login_homepage_and_unrelated_html(html: bytes) -> None:
    _assert_page_error(html)


@pytest.mark.parametrize(
    "selector",
    ["#myFrom", "#sr-holder", "ul.pagination"],
    ids=["form", "results", "paginator"],
)
def test_reklama5_parser_requires_form_results_and_paginator(selector: str) -> None:
    _assert_page_error(_without_selector(_valid_page(), selector))


@pytest.mark.parametrize("values", [[], ["1", "1"]], ids=["missing", "duplicate"])
def test_reklama5_parser_rejects_missing_or_duplicate_form_page_inputs(values: list[str]) -> None:
    _assert_page_error(replace_form_page_inputs(_valid_page(), values))


def test_reklama5_parser_accepts_page_two_with_form_reset_value_one() -> None:
    request = search_scope().page_request(2)
    page = parse_reklama5_page(
        search_page(
            2,
            [Reklama5Card(ad_id="42").html()],
            page_links=[1, 2, 3],
            result_count=1,
            active_page=2,
        ),
        request,
        FIXED_NOW,
    )

    assert page.organic_ids == {"42"}
    assert page.terminal is False


@pytest.mark.parametrize(
    "value",
    ["2", "wrong", "0", "-1", "1.0"],
    ids=["wrong-reset", "non-decimal", "zero", "negative", "fraction"],
)
def test_reklama5_parser_rejects_wrong_non_decimal_or_non_positive_form_reset_value(
    value: str,
) -> None:
    _assert_page_error(replace_form_page_inputs(_valid_page(), [value]))


def test_reklama5_parser_rejects_oversized_decimal_form_and_count_values() -> None:
    oversized_decimal = "9" * 5_000
    _assert_page_error(replace_form_page_inputs(_valid_page(), [oversized_decimal]))

    soup = BeautifulSoup(_valid_page(), "html.parser")
    count = soup.select_one('span.float-left > span[style*="vertical-align"]')
    assert count is not None
    count.string = oversized_decimal
    _assert_page_error(soup.encode())


@pytest.mark.parametrize("pages", [[], [1, 1]], ids=["missing", "duplicate"])
def test_reklama5_parser_requires_exactly_one_active_marker_when_links_exist(pages: list[int]) -> None:
    _assert_page_error(replace_active_markers(_valid_page(), pages))


def test_reklama5_parser_requires_active_page_to_match_when_links_exist() -> None:
    _assert_page_error(replace_active_markers(_valid_page(), [2]))


def test_reklama5_parser_keeps_valid_ids_from_otherwise_malformed_organic_rows() -> None:
    page = parse_reklama5_page(
        search_page(
            1,
            [Reklama5Card(ad_id="42", title="").html()],
            page_links=[],
            result_count=1,
        ),
        page_request(),
        FIXED_NOW,
    )

    assert page.organic_ids == {"42"}
    assert page.listings == ()
