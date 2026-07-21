import json

from discord_client import DiscordWebhookClient
from models import EntryData
from tests.discord_components_helpers import get_container_children, make_message


def test_components_v2_payload_renders_safe_image_as_section_and_thumbnail() -> None:
    # Given
    message = make_message(
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="Description",
            author="Author",
            timestamp="2026-07-20T12:00:00+00:00",
            image_url="https://example.test/image.png",
        ),
    )

    # When
    children = get_container_children(message)

    # Then - container children: Section, Separator, Metadata
    section = children[0]
    assert section["type"] == 9
    section_children = section["components"]
    assert isinstance(section_children, list)
    assert len(section_children) == 2
    first_child = section_children[0]
    assert isinstance(first_child, dict)
    assert first_child["type"] == 10
    assert first_child["content"] == "## [Entry](https://example.test/entry)"
    second_child = section_children[1]
    assert isinstance(second_child, dict)
    assert second_child["type"] == 10
    assert second_child["content"] == "Description"
    accessory = section["accessory"]
    assert isinstance(accessory, dict)
    assert accessory["type"] == 11
    assert accessory["media"] == {"url": "https://example.test/image.png"}
    assert accessory["description"] == "Entry"
    # Separator and metadata follow
    assert children[1]["type"] == 14
    assert children[1]["divider"] is True
    assert children[1]["spacing"] == 1
    assert children[2]["type"] == 10


def test_components_v2_payload_falls_back_to_text_display_without_image() -> None:
    # Given - no image_url
    message = make_message()

    # When
    children = get_container_children(message)

    # Then - direct Text Display, no Section
    assert children[0]["type"] == 10
    assert "accessory" not in children[0]
    assert children[1]["type"] == 10
    assert children[2]["type"] == 14
    assert children[3]["type"] == 10


def test_components_v2_payload_falls_back_for_unsafe_image_url() -> None:
    # Given - javascript: image URL
    message = make_message(
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="Description",
            author="",
            timestamp=None,
            image_url="javascript:alert(1)",
        ),
    )

    # When
    payload = DiscordWebhookClient._build_payload(message)

    # Then - no Section, no Thumbnail, direct Text Display
    children = get_container_children(message)
    assert children[0]["type"] == 10
    assert "javascript:" not in json.dumps(payload)
    # No accessory anywhere
    for child in children:
        assert "accessory" not in child


def test_components_v2_payload_caps_thumbnail_description_at_1024() -> None:
    # Given
    long_title = "T" * 5000
    message = make_message(
        entry=EntryData(
            title=long_title,
            link="https://example.test/entry",
            description="",
            author="",
            timestamp=None,
            image_url="https://example.test/image.png",
        ),
    )

    # When
    children = get_container_children(message)

    # Then
    section = children[0]
    assert section["type"] == 9
    accessory = section["accessory"]
    assert isinstance(accessory, dict)
    description = accessory["description"]
    assert isinstance(description, str)
    assert len(description) <= 1024


def test_components_v2_payload_separator_has_divider_and_compact_spacing() -> None:
    # Given
    message = make_message()

    # When
    children = get_container_children(message)

    # Then
    separator = children[2]
    assert separator["type"] == 14
    assert separator["divider"] is True
    assert separator["spacing"] == 1


def test_components_v2_payload_renders_xenforo_forum_text_layout() -> None:
    # Given - XenForo strategy, no image
    message = make_message(
        strategy="xenforo",
        url="https://forum.example.com/threads/topic.12345/",
        entry=EntryData(
            title="Thread Title",
            link="https://forum.example.com/threads/topic.12345/",
            description="Post body text",
            author="Poster",
            timestamp="2026-07-20T12:00:00+00:00",
        ),
        source_title="Forum Thread",
    )

    # When
    children = get_container_children(message)

    # Then - direct Text Display (no Section), Forum label
    assert children[0]["type"] == 10
    assert children[0]["content"] == (
        "## [Thread Title](https://forum.example.com/threads/topic.12345/)"
    )
    assert children[1]["type"] == 10
    assert children[1]["content"] == "Post body text"
    assert children[2]["type"] == 14
    metadata = children[3]["content"]
    assert isinstance(metadata, str)
    assert metadata.startswith("-# Forum • Forum Thread • By Poster •")
