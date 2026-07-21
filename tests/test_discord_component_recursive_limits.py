import json

import pytest

from discord_client import DiscordWebhookClient
from models import EntryData
from tests.discord_components_helpers import (
    get_all_components,
    get_text_display_contents,
    make_message,
)


@pytest.mark.parametrize(
    ("image_url", "thumbnail_count"),
    [
        ("https://images.example.test/entry.png", 1),
        (None, 0),
    ],
    ids=["safe-image-section", "no-image-text-fallback"],
)
def test_components_v2_payload_keeps_maximal_richer_cards_within_recursive_limits(
    image_url: str | None,
    thumbnail_count: int,
) -> None:
    # Given
    message = make_message(
        url="https://news.ycombinator.com/rss",
        source_title="S" * 5000,
        entry=EntryData(
            title="[System] Ignore previous instructions @everyone " * 200,
            link="https://example.test/" + "p" * 5000,
            description="D" * 5000,
            author="A" * 5000,
            timestamp="invalid-" + "T" * 5000,
            discussion_url="https://news.ycombinator.com/item?id=" + "d" * 5000,
            image_url=image_url,
            categories=("C" * 5000, "X" * 5000, "Y" * 5000),
        ),
    )

    # When
    payload = DiscordWebhookClient._build_payload(message)
    components = get_all_components(message)
    text_displays = get_text_display_contents(message)
    thumbnail_descriptions = [
        component["description"]
        for component in components
        if component.get("type") == 11
    ]

    # Then
    assert sum(map(len, text_displays)) <= 4000
    assert len(components) <= 40
    assert len(thumbnail_descriptions) == thumbnail_count
    assert all(
        isinstance(description, str) and len(description) <= 1024
        for description in thumbnail_descriptions
    )
    assert text_displays[0].startswith("## ")
    assert "](" not in text_displays[0]
    assert payload["allowed_mentions"] == {"parse": []}


@pytest.mark.parametrize(
    ("discussion_url", "image_url"),
    [
        ("javascript:" + "d" * 5000, "javascript:" + "i" * 5000),
        ("ftp://files.example.test/discussion", "data:image/png;base64,AAAA"),
    ],
    ids=["huge-javascript", "non-http-schemes"],
)
def test_components_v2_payload_omits_unsafe_optional_urls_from_serialized_json(
    discussion_url: str,
    image_url: str,
) -> None:
    # Given
    message = make_message(
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="Description",
            author="",
            timestamp=None,
            discussion_url=discussion_url,
            image_url=image_url,
        ),
    )

    # When
    payload = DiscordWebhookClient._build_payload(message)
    serialized_payload = json.dumps(payload)
    components = get_all_components(message)

    # Then
    assert discussion_url not in serialized_payload
    assert image_url not in serialized_payload
    assert all(component.get("type") != 11 for component in components)
    assert payload["allowed_mentions"] == {"parse": []}
