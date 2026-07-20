from dataclasses import replace

import pytest

from tests.discord_components_helpers import get_text_display_contents, make_message


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
        ("[" * 5000, "News", [3992, 7], None, "-# News"),
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
    assert contents[-1] == "-# News"
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
