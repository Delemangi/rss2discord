import json
from dataclasses import replace

from rss2discord.discord.components import (
    MAX_FOOTER_LINE_CHARACTERS,
    MAX_HEADING_CHARACTERS,
    MAX_HEADING_TITLE_CHARACTERS,
    MAX_TEXT_DISPLAY_CHARACTERS,
)
from rss2discord.models import EntryData, PriceDirection, SourceMetric
from tests.discord_components_helpers import (
    build_payload,
    get_container_children,
    get_text_display_contents,
    make_message,
)


def test_components_v2_payload_baseline_top_level_contract() -> None:
    # Given
    message = make_message()
    unsafe_message = replace(
        message,
        entry=replace(message.entry, link="javascript:alert(1)"),
    )

    # When
    payload = build_payload(message)
    contents = get_text_display_contents(message)
    children = get_container_children(message)
    unsafe_payload = build_payload(unsafe_message)
    unsafe_children = get_container_children(unsafe_message)

    # Then
    assert sum(map(len, contents)) <= MAX_TEXT_DISPLAY_CHARACTERS
    assert [child["type"] for child in children] == [10, 10, 14, 10]
    assert payload["allowed_mentions"] == {"parse": []}
    assert unsafe_children[0]["content"] == "## Entry"
    assert "javascript:" not in json.dumps(unsafe_payload)


def test_components_v2_payload_recursively_collects_section_text_displays() -> None:
    # Given
    message = make_message(
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="Description",
            author="",
            timestamp=None,
            image_url="https://example.test/image.png",
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then
    assert contents == [
        "## [Entry](https://example.test/entry)",
        "Description",
        "-# RSS • News",
    ]


def test_components_v2_payload_preserves_markdown_when_description_is_truncated() -> (
    None
):
    # Given
    description = "**Release notes** " + "D" * 5000
    message = make_message()
    message = replace(message, entry=replace(message.entry, description=description))

    # When
    contents = get_text_display_contents(message)

    # Then - the description absorbs whatever the other blocks leave behind
    assert contents[1].startswith("**Release notes** ")
    assert sum(map(len, contents)) == MAX_TEXT_DISPLAY_CHARACTERS


def test_components_v2_payload_stays_within_combined_text_limit() -> None:
    # Given - every field hostile at once
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            title="T" * 5000,
            link="https://example.test/" + "l" * 5000,
            description="D" * 5000,
            author="A" * 5000,
            timestamp="invalid-" + "X" * 5000,
            source_metrics=(SourceMetric(label="L" * 5000, value="V" * 5000),),
        ),
        source_title="S" * 5000,
    )

    # When
    contents = get_text_display_contents(message)

    # Then - each block stays inside its own ceiling and the total is exact
    assert contents
    assert all(contents)
    assert sum(map(len, contents)) == MAX_TEXT_DISPLAY_CHARACTERS
    assert len(contents[0]) <= MAX_HEADING_CHARACTERS
    assert len(contents[-1]) <= MAX_FOOTER_LINE_CHARACTERS
    # Then - an unfittable link is dropped rather than truncated into a ruin
    assert contents[0].startswith("## ")
    assert "](" not in contents[0]


def test_components_v2_payload_caps_heading_without_starving_footer() -> None:
    # Given - only the title is oversized
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            title="T" * 5000,
            link="",
            description="",
            author="",
            timestamp=None,
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then - the visible title is bounded, so the footer still renders
    assert len(contents[0]) == len("## ") + MAX_HEADING_TITLE_CHARACTERS
    assert contents[0].startswith("## ")
    assert contents[-1] == "-# RSS • News"


def test_components_v2_payload_truncates_unbalanced_heading_between_escapes() -> None:
    # Given - a title with more literal brackets than the heading budget
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            title="[" * 5000,
            link="",
            description="",
            author="",
            timestamp=None,
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then - unbalanced input keeps the conservative escaping and remains bounded
    body = contents[0].removeprefix("## ").removesuffix("…")
    assert len(contents[0]) <= MAX_HEADING_CHARACTERS
    assert body == "\\[" * (len(body) // 2)


def test_components_v2_payload_falls_back_when_truncation_unbalances_label() -> None:
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            title="[aa]" * 1000,
            link="https://example.test/entry",
            description="",
            author="",
            timestamp=None,
        ),
    )

    heading = get_text_display_contents(message)[0]

    assert "\\[" in heading
    assert len(heading) <= MAX_HEADING_CHARACTERS


def test_components_v2_payload_drops_footer_parts_that_do_not_fit() -> None:
    # Given - a source title far past the footer ceiling
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            title="Entry",
            link="",
            description="",
            author="",
            timestamp=None,
        ),
        source_title="S" * 5000,
    )

    # When
    contents = get_text_display_contents(message)

    # Then - whole parts drop out, leaving no half-rendered fragment behind
    assert contents[-1] == "-# RSS"


def test_components_v2_payload_keeps_timestamp_after_overflowing_categories() -> None:
    # Given - more categories than the footer can hold
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            description="",
            author="",
            categories=tuple("C" * 200 for _ in range(20)),
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then - the reserved tail means the time survives the categories
    assert contents[-1].endswith("<t:1784548800:R>")
    assert len(contents[-1]) <= MAX_FOOTER_LINE_CHARACTERS


def test_components_v2_payload_drops_link_that_exceeds_text_budget() -> None:
    # Given
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            link="https://example.test/" + "l" * 5000,
            description="",
            author="",
            timestamp=None,
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then
    assert contents[0] == "## Entry"


def test_components_v2_payload_keeps_the_link_when_only_the_url_is_long() -> None:
    # Given - a realistic long title behind a URL carrying tracking parameters
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            title="T" * 220,
            link="https://example.test/product?" + "utm=x&" * 50,
            description="",
            author="",
            timestamp=None,
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then - a long address costs budget but no width, so the link survives
    assert contents[0].startswith("## [")
    assert "](" in contents[0]
    assert len(contents[0]) <= MAX_HEADING_CHARACTERS


def test_components_v2_payload_skips_an_oversized_footer_part() -> None:
    # Given - one category far past the ceiling, between two that fit
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            description="",
            author="",
            timestamp=None,
            categories=("first", "X" * 600, "last"),
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then - the overlong one drops out without taking the rest with it
    assert contents[-1] == "-# RSS • News • first • last"


def test_components_v2_payload_keeps_a_title_on_one_line() -> None:
    # Given - a title that tries to close the heading and forge a footer of its
    # own, styled exactly like the card's real provenance line
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            title="Real Story\n-# RSS • Verified Source",
            link="",
            description="",
            author="",
            timestamp=None,
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then - a heading occupies one line, so no counterfeit line can appear
    assert contents[0] == "## Real Story -# RSS • Verified Source"
    assert "\n" not in contents[0]


def test_components_v2_payload_without_discount_is_unchanged() -> None:
    # Given - a product card with price details but no discount field
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="Description",
            author="Author",
            timestamp="2026-07-20T12:00:00+00:00",
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Original", "100 MKD"),
            ),
        ),
    )

    # When
    payload = build_payload(message)

    # Then - exact payload proves the discount-free path is untouched
    assert payload == {
        "allowed_mentions": {"parse": []},
        "avatar_url": "https://example.test/avatar.png",
        "components": [
            {
                "accent_color": 5814783,
                "components": [
                    {
                        "content": "## [Item](https://example.test/item)",
                        "type": 10,
                    },
                    {
                        "content": "Price: **80 MKD**\n-# \u200b\n-# Original: 100 MKD",
                        "type": 10,
                    },
                    {"content": "Description", "type": 10},
                    {"divider": True, "spacing": 1, "type": 14},
                    {
                        "content": "-# RSS • News • By Author • <t:1784548800:R>",
                        "type": 10,
                    },
                ],
                "type": 17,
            },
        ],
        "flags": 32768,
        "username": "RSS Bot",
    }


def test_discount_badge_respects_text_limits() -> None:
    # Given - a discounted product card where every text field is hostile
    message = make_message(
        entry=EntryData(
            title="T" * 5000,
            link="https://example.test/item",
            description="D" * 5000,
            author="",
            timestamp=None,
            source_metrics=(SourceMetric("Price", "80 MKD"),),
            discount_percentage="20.0%",
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then - the badge is visible and every text block stays inside its budgets
    assert sum(map(len, contents)) <= MAX_TEXT_DISPLAY_CHARACTERS
    assert len(contents[0]) <= MAX_HEADING_CHARACTERS
    assert contents[-1] == "-# RSS • News"
    assert len(contents[-1]) <= MAX_FOOTER_LINE_CHARACTERS
    assert any("**20% off**" in content for content in contents)


def test_price_change_card_without_history_fields_is_unchanged() -> None:
    # Given - a price-change alert that carries no history context
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="Description",
            author="Author",
            timestamp="2026-07-20T12:00:00+00:00",
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Previous", "90 MKD", prior=True),
                SourceMetric("Original", "100 MKD"),
            ),
            price_change_percentage="-11.1%",
            discount_percentage="20.0%",
            price_direction=PriceDirection.DECREASE,
        ),
    )

    # When
    payload = build_payload(message)

    # Then - exact payload proves the history-free path is untouched
    assert payload == {
        "allowed_mentions": {"parse": []},
        "components": [
            {
                "accent_color": 3908957,
                "components": [
                    {
                        "content": "## [Price change: Item](https://example.test/item)",
                        "type": 10,
                    },
                    {
                        "content": "Price: **80 MKD** ~~90 MKD~~\n-# \u200b\n-# Change: -11.1%\n-# Original: 100 MKD\n-# Discount: 20% off",
                        "type": 10,
                    },
                    {"content": "Description", "type": 10},
                    {"divider": True, "spacing": 1, "type": 14},
                    {"content": "-# RSS • News • By Author", "type": 10},
                ],
                "type": 17,
            },
        ],
        "flags": 32768,
        "username": "RSS Bot",
        "avatar_url": "https://example.test/avatar.png",
    }


def test_period_low_badge_respects_text_limits() -> None:
    # Given - a hostile price-drop card at the period low
    message = make_message(
        entry=EntryData(
            title="T" * 5000,
            link="https://example.test/item",
            description="D" * 5000,
            author="",
            timestamp=None,
            source_metrics=(SourceMetric("Price", "80 MKD"),),
            price_change_percentage="-11.1%",
            price_direction=PriceDirection.DECREASE,
            price_period_low="80 MKD",
            price_period_high="120 MKD",
            price_is_period_low=True,
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then - the badge is visible and every text block stays inside its budgets
    assert sum(map(len, contents)) <= MAX_TEXT_DISPLAY_CHARACTERS
    assert len(contents[0]) <= MAX_HEADING_CHARACTERS
    assert contents[-1] == "-# RSS • News"
    assert len(contents[-1]) <= MAX_FOOTER_LINE_CHARACTERS
    assert any("**30-day low**" in content for content in contents)


def test_restocked_false_price_change_card_is_unchanged() -> None:
    # Given - a normal price-change alert explicitly marked as not a restock
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="Description",
            author="Author",
            timestamp="2026-07-20T12:00:00+00:00",
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Previous", "90 MKD", prior=True),
                SourceMetric("Original", "100 MKD"),
            ),
            price_change_percentage="-11.1%",
            discount_percentage="20.0%",
            price_direction=PriceDirection.DECREASE,
            restocked=False,
        ),
    )

    # When
    payload = build_payload(message)

    # Then - exact payload proves the restock path did not touch normal alerts
    assert payload == {
        "allowed_mentions": {"parse": []},
        "components": [
            {
                "accent_color": 3908957,
                "components": [
                    {
                        "content": "## [Price change: Item](https://example.test/item)",
                        "type": 10,
                    },
                    {
                        "content": "Price: **80 MKD** ~~90 MKD~~\n-# \u200b\n-# Change: -11.1%\n-# Original: 100 MKD\n-# Discount: 20% off",
                        "type": 10,
                    },
                    {"content": "Description", "type": 10},
                    {"divider": True, "spacing": 1, "type": 14},
                    {"content": "-# RSS • News • By Author", "type": 10},
                ],
                "type": 17,
            },
        ],
        "flags": 32768,
        "username": "RSS Bot",
        "avatar_url": "https://example.test/avatar.png",
    }


def test_restocked_false_discovery_card_is_unchanged() -> None:
    # Given - a product discovery card explicitly marked as not a restock
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="Description",
            author="Author",
            timestamp="2026-07-20T12:00:00+00:00",
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Original", "100 MKD"),
            ),
            discount_percentage="20.0%",
            restocked=False,
        ),
    )

    # When
    payload = build_payload(message)

    # Then - exact payload proves discovery rendering is untouched
    assert payload == {
        "allowed_mentions": {"parse": []},
        "components": [
            {
                "accent_color": 5814783,
                "components": [
                    {
                        "content": "## [Item](https://example.test/item)",
                        "type": 10,
                    },
                    {
                        "content": "Price: **80 MKD** · **20% off**\n-# \u200b\n-# Original: 100 MKD",
                        "type": 10,
                    },
                    {"content": "Description", "type": 10},
                    {"divider": True, "spacing": 1, "type": 14},
                    {
                        "content": "-# RSS • News • By Author • <t:1784548800:R>",
                        "type": 10,
                    },
                ],
                "type": 17,
            },
        ],
        "flags": 32768,
        "username": "RSS Bot",
        "avatar_url": "https://example.test/avatar.png",
    }


def test_restock_badge_respects_text_limits() -> None:
    # Given - a restock card where every text field is hostile
    message = make_message(
        entry=EntryData(
            title="T" * 5000,
            link="https://example.test/item",
            description="D" * 5000,
            author="",
            timestamp=None,
            source_metrics=(SourceMetric("Price", "80 MKD"),),
            restocked=True,
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then - the badge is visible and every text block stays inside its budgets
    assert sum(map(len, contents)) <= MAX_TEXT_DISPLAY_CHARACTERS
    assert len(contents[0]) <= MAX_HEADING_CHARACTERS
    assert contents[-1] == "-# RSS • News"
    assert len(contents[-1]) <= MAX_FOOTER_LINE_CHARACTERS
    assert any("**Back in stock**" in content for content in contents)


def test_combined_restock_and_period_low_badges_respect_text_limits() -> None:
    # Given - a restock at the period low where every text field is hostile
    message = make_message(
        entry=EntryData(
            title="T" * 5000,
            link="https://example.test/item",
            description="D" * 5000,
            author="",
            timestamp=None,
            source_metrics=(SourceMetric("Price", "80 MKD"),),
            restocked=True,
            price_change_percentage="-11.1%",
            price_direction=PriceDirection.DECREASE,
            price_period_low="80 MKD",
            price_period_high="120 MKD",
            price_is_period_low=True,
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then - both badges are visible and every text block stays inside its budgets
    assert sum(map(len, contents)) <= MAX_TEXT_DISPLAY_CHARACTERS
    assert len(contents[0]) <= MAX_HEADING_CHARACTERS
    assert contents[-1] == "-# RSS • News"
    assert len(contents[-1]) <= MAX_FOOTER_LINE_CHARACTERS
    assert any("**Back in stock**" in content for content in contents)
    assert any("**30-day low**" in content for content in contents)


def test_period_range_respects_text_limits() -> None:
    # Given - a hostile price-drop card with a usable range
    message = make_message(
        entry=EntryData(
            title="T" * 5000,
            link="https://example.test/item",
            description="D" * 5000,
            author="",
            timestamp=None,
            source_metrics=(SourceMetric("Price", "85 MKD"),),
            price_change_percentage="-5.6%",
            price_direction=PriceDirection.DECREASE,
            price_period_low="80 MKD",
            price_period_high="120 MKD",
            price_is_period_low=False,
        ),
    )

    # When
    contents = get_text_display_contents(message)

    # Then - the range line is visible and every text block stays inside its budgets
    assert sum(map(len, contents)) <= MAX_TEXT_DISPLAY_CHARACTERS
    assert len(contents[0]) <= MAX_HEADING_CHARACTERS
    assert contents[-1] == "-# RSS • News"
    assert len(contents[-1]) <= MAX_FOOTER_LINE_CHARACTERS
    assert any("30-day range: 80 MKD – 120 MKD" in content for content in contents)
