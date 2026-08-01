from datetime import UTC, datetime, timedelta

import pytest

from rss2discord.models import EntryId
from rss2discord.transports import FeedFetchError
from rss2discord.transports.reklama5 import (
    SKOPJE,
    Reklama5Listing,
    Reklama5Page,
    parse_reklama5_page,
)
from tests.reklama5_helpers import (
    FIXED_NOW,
    Reklama5Card,
    fixture_html,
    page_request,
    search_page,
)


def parse_cards(*cards: Reklama5Card, now: datetime = FIXED_NOW) -> Reklama5Page:
    return parse_reklama5_page(search_page(*cards), page_request(), now)


def test_reklama5_parser_maps_a_rich_generic_card() -> None:
    page = parse_reklama5_page(fixture_html("rich_page.html"), page_request(), FIXED_NOW)
    listing = page.listings[0]

    assert listing == Reklama5Listing(
        entry_id=EntryId("1234567"),
        url="https://reklama5.mk/AdDetails?ad=1234567",
        title="Normalized title",
        summary="Normalized description",
        price="По Договор",
        location="Скопје",
        category="Компјутерски делови и опрема",
        activity_at=datetime(2026, 8, 1, 10, 30, tzinfo=SKOPJE),
        image_url="https://reklama5.mk/images/item.jpg",
    )


def test_reklama5_parser_preserves_arbitrary_category_behavior() -> None:
    request = page_request("https://reklama5.mk/Search?cat=99&city=bitola&custom=x")
    page = parse_reklama5_page(fixture_html("generic_page.html"), request, FIXED_NOW)

    assert page.listings[0].category == "Велосипеди"
    assert page.listings[0].entry_id == "7654321"


def test_reklama5_parser_normalizes_visible_whitespace() -> None:
    listing = parse_cards(Reklama5Card(title=" A \n B ", summary=" C\t D ")).listings[0]

    assert (listing.title, listing.summary) == ("A B", "C D")


def test_reklama5_parser_truncates_normalized_summary_to_2000_characters() -> None:
    listing = parse_cards(Reklama5Card(summary=("word   " * 600))).listings[0]

    assert len(listing.summary) == 2000
    assert listing.summary.endswith("...")


def test_reklama5_parser_maps_missing_optional_values_to_empty_values() -> None:
    listing = parse_cards(Reklama5Card(price="", location="", category="", image=None)).listings[0]

    assert (listing.price, listing.location, listing.category, listing.image_url) == ("", "", "", None)


def test_reklama5_parser_keeps_highlighted_organic_cards() -> None:
    page = parse_cards(Reklama5Card(ad_id="42", highlighted=True))

    assert page.organic_ids == {"42"}
    assert page.listings[0].entry_id == "42"


def test_reklama5_parser_excludes_carousel_links_and_skips_malformed_cards() -> None:
    page = parse_reklama5_page(fixture_html("rich_page.html"), page_request(), FIXED_NOW)

    assert page.organic_ids == {"1234566", "1234567", "1234568", "1234569", "1234570"}
    assert tuple(item.entry_id for item in page.listings) == ("1234567", "1234566", "1234568")


def test_reklama5_parser_canonicalizes_a_valid_detail_url() -> None:
    listing = parse_cards(Reklama5Card(href="/AdDetails?x=discard&ad=42#discard")).listings[0]

    assert listing.url == "https://reklama5.mk/AdDetails?ad=42"


@pytest.mark.parametrize(
    "href",
    ["https://www.reklama5.mk/AdDetails?ad=42", "http://reklama5.mk/AdDetails?ad=42", "/Other?ad=42", "/AdDetails?ad=42&ad=43"],
)
def test_reklama5_parser_enforces_exact_origin_detail_identity(href: str) -> None:
    assert parse_cards(Reklama5Card(href=href)).listings == ()


def test_reklama5_parser_treats_missing_promotion_marker_as_organic() -> None:
    assert parse_cards(Reklama5Card()).organic_ids == {"9000001"}


def test_reklama5_parser_skips_exact_normalized_promoted_marker() -> None:
    assert parse_cards(Reklama5Card(promotion="  Промовирано  ")).listings == ()


def test_reklama5_parser_rejects_unknown_promotion_marker() -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        parse_cards(Reklama5Card(promotion="Promoted"))

    assert fetch_error.value.cause_type == "InvalidPromotionMarker"
    assert not fetch_error.value.retryable


def test_reklama5_parser_rejects_empty_normalized_promotion_marker() -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        parse_cards(Reklama5Card(promotion="   "))

    assert fetch_error.value.cause_type == "InvalidPromotionMarker"
    assert not fetch_error.value.retryable


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("https://reklama5.mk/images/item.jpg", "https://reklama5.mk/images/item.jpg"),
        ("//cdn.example.test/image.jpg", "https://cdn.example.test/image.jpg"),
        ("https://images.example.test/item.jpg", "https://images.example.test/item.jpg"),
        ("https://images.example.test:8443/item.jpg", "https://images.example.test:8443/item.jpg"),
        ("http://images.example.test/item.jpg", None),
        ("/images/item.jpg", None),
        ("images/item.jpg", None),
    ],
)
def test_reklama5_parser_accepts_only_resolved_absolute_https_images(image: str, expected: str | None) -> None:
    assert parse_cards(Reklama5Card(image=image)).listings[0].image_url == expected


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("Денес 10:30", datetime(2026, 8, 1, 10, 30, tzinfo=SKOPJE)),
        ("Вчера 23:45", datetime(2026, 7, 31, 23, 45, tzinfo=SKOPJE)),
        ("14/02/2024 08:05", datetime(2024, 2, 14, 8, 5, tzinfo=SKOPJE)),
    ],
)
def test_reklama5_parser_parses_relative_and_calendar_timestamps(timestamp: str, expected: datetime) -> None:
    assert parse_cards(Reklama5Card(timestamp=timestamp)).listings[0].activity_at == expected


@pytest.mark.parametrize(
    ("token", "month", "year"),
    [("јан", 1, 2026), ("фев", 2, 2026), ("мар", 3, 2026), ("апр", 4, 2026), ("мај", 5, 2026), ("јун", 6, 2026), ("јул", 7, 2026), ("авг", 8, 2026), ("сеп", 9, 2025), ("окт", 10, 2025), ("ное", 11, 2025), ("дек", 12, 2025)],
)
def test_reklama5_parser_parses_all_site_month_tokens(token: str, month: int, year: int) -> None:
    parsed = parse_cards(Reklama5Card(timestamp=f"1 {token} 09:15")).listings[0].activity_at

    assert parsed == datetime(year, month, 1, 9, 15, tzinfo=SKOPJE)


def test_reklama5_parser_rolls_a_future_month_only_timestamp_to_previous_year() -> None:
    parsed = parse_cards(Reklama5Card(timestamp="2 авг 13:00")).listings[0].activity_at

    assert parsed == datetime(2025, 8, 2, 13, 0, tzinfo=SKOPJE)


def test_reklama5_parser_uses_fold_zero_for_ambiguous_local_time() -> None:
    parsed = parse_cards(Reklama5Card(timestamp="25/10/2026 02:30")).listings[0].activity_at

    assert parsed.fold == 0
    assert parsed.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize("timestamp", ["29/03/2026 02:30", "not a timestamp"])
def test_reklama5_parser_skips_nonexistent_and_unparseable_timestamps(timestamp: str) -> None:
    page = parse_cards(Reklama5Card(timestamp=timestamp))

    assert page.listings == ()
    assert page.organic_ids == {"9000001"}


def test_reklama5_parser_rejects_a_naive_injected_clock() -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        parse_cards(Reklama5Card(), now=FIXED_NOW.replace(tzinfo=None))

    assert fetch_error.value.cause_type == "InvalidClock"


def test_reklama5_parser_converts_aware_now_before_today_rules() -> None:
    now = datetime(2026, 8, 1, 22, 30, tzinfo=UTC)
    parsed = parse_cards(Reklama5Card(timestamp="Денес 00:15"), now=now).listings[0].activity_at

    assert parsed == datetime(2026, 8, 2, 0, 15, tzinfo=SKOPJE)
