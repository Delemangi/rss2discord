import json
from dataclasses import replace

import pytest

from discord_client import DiscordWebhookClient
from models import EntryData
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
    assert sum(map(len, contents)) <= 4000
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

    # Then
    assert contents[1].startswith("**Release notes** ")
    assert len(contents[1]) == 3992


def test_components_v2_payload_stays_within_combined_text_limit() -> None:
    # Given
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
        ),
        source_title="S" * 5000,
    )

    # When
    contents = get_text_display_contents(message)

    # Then
    assert contents
    assert all(contents)
    assert list(map(len, contents)) == [4, 3992, 4]


@pytest.mark.parametrize(
    (
        "title",
        "source_title",
        "expected_lengths",
        "expected_heading",
        "expected_metadata",
    ),
    [
        ("Entry", "S" * 5000, [8, 3992], "## Entry", None),
        ("T" * 3000, "S" * 3000, [3003, 997], "## " + "T" * 3000, None),
        ("T" * 5000, "S" * 5000, [2000, 2000], None, None),
        ("[" * 5000, "News", [3986, 13], None, "-# RSS • News"),
    ],
    ids=[
        "metadata-only-oversized",
        "combined-overflow-preserves-heading",
        "both-fields-oversized",
        "escaped-title-oversized",
    ],
)
def test_components_v2_payload_allocates_heading_and_metadata_budget(
    title: str,
    source_title: str,
    expected_lengths: list[int],
    expected_heading: str | None,
    expected_metadata: str | None,
) -> None:
    # Given
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            title=title,
            link="",
            description="",
            author="",
            timestamp=None,
        ),
        source_title=source_title,
    )

    # When
    contents = get_text_display_contents(message)

    # Then
    assert list(map(len, contents)) == expected_lengths
    if expected_heading is not None:
        assert contents[0] == expected_heading
    if expected_metadata is not None:
        assert contents[-1] == expected_metadata


def test_components_v2_payload_preserves_metadata_when_only_title_is_oversized() -> (
    None
):
    # Given
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

    # Then
    assert contents[-1] == "-# RSS • News"
    assert sum(map(len, contents)) == 4000


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
