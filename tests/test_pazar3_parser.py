from datetime import datetime

import pytest

from rss2discord.models import EntryId
from rss2discord.transports import FeedFetchError
from rss2discord.transports.pazar3_models import Pazar3Listing
from rss2discord.transports.pazar3_parser import parse_pazar3_page
from tests.pazar3_helpers import (
    FIXED_NOW,
    SEARCH_URL,
    Pazar3Card,
    listing_page,
    page_request,
)


def parse_cards(
    *cards: Pazar3Card,
    top_cards: tuple[Pazar3Card, ...] = (),
) -> tuple[Pazar3Listing, ...]:
    return parse_pazar3_page(
        listing_page(
            1,
            [card.html() for card in cards],
            total=len(cards),
            top_cards=[card.html() for card in top_cards],
        ),
        page_request(),
        FIXED_NOW,
    ).listings


def test_pazar3_parser_maps_an_organic_listing_card() -> None:
    image = (
        "https://media.pazar3.mk/Image/id/20260804/665/500/monitor.jpeg"
        "?AllowCropping=False"
    )

    listing = parse_cards(Pazar3Card(image=image))[0]

    assert listing == Pazar3Listing(
        entry_id=EntryId("9086886"),
        url=(
            "https://www.pazar3.mk/oglas/elektronika/"
            "delovi-za-kompjuteri-dodatoci/prodazba/skopje/aerodrom/"
            "test-listing/9086886"
        ),
        title="Се продава AOC монитор.",
        price="6 000 МКД",
        location="Скопjе, Аеродром",
        category="Делови за Компјутери и додатоци",
        activity_at=datetime.fromisoformat("2026-08-05T11:30:00+02:00"),
        image_url=image,
    )


def test_pazar3_parser_excludes_top_placement_but_keeps_organic_duplicate() -> None:
    page = parse_pazar3_page(
        listing_page(
            1,
            [Pazar3Card(product_id="42", title="Organic").html()],
            total=1,
            top_cards=[Pazar3Card(product_id="42", title="Promoted").html()],
        ),
        page_request(),
        FIXED_NOW,
    )

    assert page.organic_ids == {"42"}
    assert [listing.title for listing in page.listings] == ["Organic"]
    assert page.organic_row_count == 1


def test_pazar3_parser_retains_valid_id_when_mutable_fields_are_malformed() -> None:
    page = parse_pazar3_page(
        listing_page(1, [Pazar3Card(title="", product_id="42").html()], total=1),
        page_request(),
        FIXED_NOW,
    )

    assert page.organic_ids == {"42"}
    assert page.listings == ()


@pytest.mark.parametrize(
    "card",
    [
        Pazar3Card(product_id="42", href="/oglas/elektronika/item/43"),
        Pazar3Card(product_id="042", href="/oglas/elektronika/item/042"),
        Pazar3Card(product_id="٤٢", href="/oglas/elektronika/item/٤٢"),
        Pazar3Card(product_id=" 42 ", href="/oglas/elektronika/item/42"),
        Pazar3Card(product_id="42", href="https://example.test/oglas/item/42"),
        Pazar3Card(product_id="42", href="/oglas/../../outside/42"),
        Pazar3Card(product_id="42", href="/oglas/%2e%2e/%2e%2e/outside/42"),
    ],
)
def test_pazar3_parser_rejects_invalid_listing_identity(card: Pazar3Card) -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        parse_cards(card)

    assert fetch_error.value.cause_type == "InvalidListingIdentity"


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("Денес 10:30", "2026-08-05T10:30:00+02:00"),
        ("Вчера 23:45", "2026-08-04T23:45:00+02:00"),
        ("27 јул. 14:09", "2026-07-27T14:09:00+02:00"),
        ("31 дек. 23:00", "2025-12-31T23:00:00+01:00"),
    ],
)
def test_pazar3_parser_parses_localized_display_timestamps(
    timestamp: str,
    expected: str,
) -> None:
    assert parse_cards(Pazar3Card(timestamp=timestamp))[0].activity_at == (
        datetime.fromisoformat(expected)
    )


def test_pazar3_parser_rejects_non_media_image_host() -> None:
    assert (
        parse_cards(Pazar3Card(image="https://example.test/image.jpg"))[0].image_url
        is None
    )


def test_pazar3_parser_reports_terminal_page_from_result_range() -> None:
    page = parse_pazar3_page(
        listing_page(1, [Pazar3Card().html()], total=1),
        page_request(),
        FIXED_NOW,
    )

    assert page.result_count == 1
    assert page.terminal


def test_pazar3_parser_accepts_canonical_page_one_paginator_link() -> None:
    html = listing_page(1, [Pazar3Card().html()], total=1).replace(
        f"{SEARCH_URL}?Page=1".encode(),
        SEARCH_URL.encode(),
        1,
    )

    page = parse_pazar3_page(html, page_request(), FIXED_NOW)

    assert page.organic_ids == {"9086886"}


def test_pazar3_parser_accepts_relative_paginator_link() -> None:
    cards = [
        Pazar3Card(product_id=str(product_id)).html() for product_id in range(1, 51)
    ]
    html = listing_page(1, cards, total=51).replace(
        f"{SEARCH_URL}?Page=2".encode(),
        b"?Page=2",
        1,
    )

    page = parse_pazar3_page(html, page_request(), FIXED_NOW)

    assert page.result_count == 51


def test_pazar3_parser_rejects_more_than_three_promoted_rows() -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        parse_pazar3_page(
            listing_page(
                1,
                [Pazar3Card().html()],
                total=1,
                top_cards=[
                    Pazar3Card(product_id=str(product_id)).html()
                    for product_id in range(1, 5)
                ],
            ),
            page_request(),
            FIXED_NOW,
        )

    assert fetch_error.value.cause_type == "InvalidPromotedListings"


def test_pazar3_parser_rejects_zero_results_with_forward_page() -> None:
    html = listing_page(1, [], total=0).replace(
        b'</div><li class="active">',
        (
            f'<a class="page-number" page-no="2" '
            f'href="{SEARCH_URL}?Page=2">2</a></div><li class="active">'
        ).encode(),
        1,
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        parse_pazar3_page(html, page_request(), FIXED_NOW)

    assert fetch_error.value.cause_type == "InvalidPaginator"


def test_pazar3_parser_rejects_short_non_terminal_page() -> None:
    cards = [
        Pazar3Card(product_id=str(product_id)).html() for product_id in range(1, 50)
    ]

    with pytest.raises(FeedFetchError) as fetch_error:
        parse_pazar3_page(listing_page(1, cards, total=100), page_request(), FIXED_NOW)

    assert fetch_error.value.cause_type == "InvalidPage"


def test_pazar3_parser_rejects_malformed_paginator_anchor() -> None:
    cards = [
        Pazar3Card(product_id=str(product_id)).html() for product_id in range(1, 51)
    ]
    html = listing_page(1, cards, total=51).replace(
        b' page-no="2"',
        b"",
        1,
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        parse_pazar3_page(html, page_request(), FIXED_NOW)

    assert fetch_error.value.cause_type == "InvalidPaginator"


def test_pazar3_parser_rejects_terminal_page_above_page_size() -> None:
    cards = [
        Pazar3Card(product_id=str(product_id)).html() for product_id in range(1, 52)
    ]
    page_two_link = (
        f'<a class="page-number" page-no="2" href="{SEARCH_URL}?Page=2">2</a>'
    ).encode()
    html = listing_page(1, cards, total=51).replace(page_two_link, b"", 1)

    with pytest.raises(FeedFetchError) as fetch_error:
        parse_pazar3_page(html, page_request(), FIXED_NOW)

    assert fetch_error.value.cause_type == "InvalidPage"


def test_pazar3_parser_rejects_non_newest_sort() -> None:
    with pytest.raises(FeedFetchError) as fetch_error:
        parse_pazar3_page(
            listing_page(
                1,
                [Pazar3Card().html()],
                total=1,
                active_sort="PriceAsc",
            ),
            page_request(),
            FIXED_NOW,
        )

    assert fetch_error.value.cause_type == "InvalidPage"
