import json
from dataclasses import replace

from rss2discord.discord.client import DiscordWebhookClient
from rss2discord.discord.components import (
    MAX_FOOTER_LINE_CHARACTERS,
    MAX_HEADING_CHARACTERS,
    MAX_HEADING_TITLE_CHARACTERS,
    MAX_TEXT_DISPLAY_CHARACTERS,
)
from rss2discord.models import EntryData, SourceMetric
from tests.discord_components_helpers import (
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
    payload = DiscordWebhookClient._build_payload(message)
    contents = get_text_display_contents(message)
    children = get_container_children(message)
    unsafe_payload = DiscordWebhookClient._build_payload(unsafe_message)
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


def test_components_v2_payload_truncates_escaped_heading_between_escapes() -> None:
    # Given - a title that doubles in length once escaped
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

    # Then - the cut never splits a backslash from the character it escapes
    body = contents[0].removeprefix("## ").removesuffix("…")
    assert len(contents[0]) <= MAX_HEADING_CHARACTERS
    assert body == "\\[" * (len(body) // 2)


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
