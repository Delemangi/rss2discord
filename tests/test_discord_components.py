import json
from dataclasses import replace

import pytest

from rss2discord.discord.client import DiscordWebhookClient
from tests.discord_components_helpers import (
    get_container_children,
    get_metadata_content,
    get_text_display_contents,
    make_message,
)


def test_components_v2_payload_preserves_webhook_compatibility_fields() -> None:
    # Baseline characterization: invariant aspects that must survive the
    # richer-card renderer change. Passes before and after production edits.
    # Given
    message = make_message(embed_color=0)

    # When
    payload = DiscordWebhookClient._build_payload(message)

    # Then - flags, mentions, username, avatar, accent color
    assert payload["flags"] == 32768
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["username"] == "RSS Bot"
    assert payload["avatar_url"] == "https://example.test/avatar.png"
    children = get_container_children(message)
    assert children[0]["type"] == 10
    assert children[0]["content"] == "## [Entry](https://example.test/entry)"
    assert children[1]["type"] == 10
    assert children[1]["content"] == "Description"
    assert children[2]["type"] == 14
    assert children[3]["type"] == 10
    # Then - title-link fallback for unsafe link
    unsafe_message = make_message()
    unsafe_message = replace(
        unsafe_message,
        entry=replace(unsafe_message.entry, link="javascript:alert(1)"),
    )
    unsafe_children = get_container_children(unsafe_message)
    assert unsafe_children[0]["content"] == "## Entry"
    # Then - no javascript: anywhere in serialized payload
    unsafe_payload = DiscordWebhookClient._build_payload(unsafe_message)
    assert "javascript:" not in json.dumps(unsafe_payload)


def test_components_v2_payload_preserves_entry_and_webhook_fields() -> None:
    # Given
    message = make_message(embed_color=0)

    # When
    payload = DiscordWebhookClient._build_payload(message)

    # Then
    assert payload == {
        "allowed_mentions": {"parse": []},
        "avatar_url": "https://example.test/avatar.png",
        "components": [
            {
                "accent_color": 0,
                "components": [
                    {
                        "content": "## [Entry](https://example.test/entry)",
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


def test_components_v2_payload_omits_empty_optional_text() -> None:
    # Given
    message = make_message()
    message = replace(
        message,
        entry=replace(message.entry, description="", author="", timestamp=None),
    )

    # When
    payload = DiscordWebhookClient._build_payload(message)

    # Then
    components = payload["components"]
    assert isinstance(components, list)
    container = components[0]
    assert isinstance(container, dict)
    assert container["accent_color"] == 5814783
    assert container["components"] == [
        {"content": "## [Entry](https://example.test/entry)", "type": 10},
        {"divider": True, "spacing": 1, "type": 14},
        {"content": "-# RSS • News", "type": 10},
    ]


@pytest.mark.parametrize(
    ("link", "expected_heading"),
    [
        (
            "https://example.test/wiki/Foo_(bar)",
            "## [Entry](https://example.test/wiki/Foo_%28bar%29)",
        ),
        (
            "https://legit.example/) [Urgent](https://evil.example/phish",
            "## [Entry](https://legit.example/%29%20[Urgent]%28https://evil.example/phish)",
        ),
        ("javascript:alert(1)", "## Entry"),
    ],
)
def test_components_v2_payload_safely_renders_entry_links(
    link: str,
    expected_heading: str,
) -> None:
    # Given
    message = make_message()
    message = replace(message, entry=replace(message.entry, link=link))

    # When
    children = get_container_children(message)

    # Then
    heading = children[0]
    assert heading["content"] == expected_heading


@pytest.mark.parametrize(
    ("author", "source_title", "expected_metadata"),
    [
        (
            "[Official](https://evil.example)",
            "News",
            "-# RSS • News • By \\[Official\\]\\(h\u200bttps://evil.example\\) • "
            "<t:1784548800:R>",
        ),
        (
            "Author",
            "[Trusted Source](https://evil.example)",
            "-# RSS • \\[Trusted Source\\]\\(h\u200bttps://evil.example\\) • By Author • "
            "<t:1784548800:R>",
        ),
        (
            "https://evil.example/author",
            "www.evil.example/source",
            "-# RSS • w\u200bww.evil.example/source • By h\u200bttps://evil.example/author • "
            "<t:1784548800:R>",
        ),
    ],
)
def test_components_v2_payload_escapes_metadata_links(
    author: str,
    source_title: str,
    expected_metadata: str,
) -> None:
    # Given
    message = make_message()
    message = replace(
        message,
        entry=replace(message.entry, author=author),
        source_title=source_title,
    )

    # When
    metadata = get_metadata_content(message)

    # Then
    assert metadata == expected_metadata


def test_components_v2_payload_preserves_description_markdown() -> None:
    # Given
    description = "Read the [documentation](https://example.test/docs) for **details**."
    message = make_message()
    message = replace(message, entry=replace(message.entry, description=description))

    # When
    contents = get_text_display_contents(message)

    # Then
    assert contents[1] == description
