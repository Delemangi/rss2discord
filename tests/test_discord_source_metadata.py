import json
from typing import Literal

import pytest

from discord_client import DiscordWebhookClient
from models import EntryData, SourceMetric
from tests.discord_components_helpers import get_metadata_content, make_message


@pytest.mark.parametrize(
    ("strategy", "url", "expected_label"),
    [
        ("xenforo", "https://forum.example.com/threads/topic.12345/", "Forum"),
        ("rss", "https://www.reddit.com/r/python/.rss", "Reddit"),
        ("rss", "https://reddit.com/r/python/.rss", "Reddit"),
        ("rss", "https://old.reddit.com/r/python/.rss", "Reddit"),
        ("rss", "https://news.ycombinator.com/rss", "Hacker News"),
        ("rss", "https://example.test/feed.xml", "RSS"),
        ("rss", "https://blog.example.com/feed.xml", "RSS"),
    ],
    ids=[
        "xenforo-forum",
        "reddit-www",
        "reddit-bare",
        "reddit-subdomain",
        "hacker-news",
        "generic-rss",
        "other-rss",
    ],
)
def test_components_v2_payload_renders_source_label(
    strategy: Literal["rss", "xenforo"],
    url: str,
    expected_label: str,
) -> None:
    # Given
    message = make_message(
        strategy=strategy,
        url=url,
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="",
            author="",
            timestamp=None,
        ),
    )

    # When
    metadata = get_metadata_content(message)

    # Then
    assert metadata.startswith(f"-# {expected_label} • ")


def test_components_v2_payload_renders_hacker_news_discussion_link() -> None:
    # Given - HN article with distinct discussion URL
    message = make_message(
        url="https://news.ycombinator.com/rss",
        entry=EntryData(
            title="Show HN: My Project",
            link="https://example.com/project",
            description="",
            author="",
            timestamp=None,
            discussion_url="https://news.ycombinator.com/item?id=12345",
        ),
    )

    # When
    metadata = get_metadata_content(message)

    # Then
    assert "-# Hacker News • News" in metadata
    assert "[Discussion](https://news.ycombinator.com/item?id=12345)" in metadata


@pytest.mark.parametrize(
    ("adapter", "expected_label"),
    [("hackernews", "Hacker News"), ("reddit", "Reddit")],
)
def test_components_v2_payload_renders_configured_adapter_label(
    adapter: Literal["hackernews", "reddit"],
    expected_label: str,
) -> None:
    # Given
    message = make_message(
        adapter=adapter,
        url="https://feeds.example.test/source.xml",
    )

    # When
    metadata = get_metadata_content(message)

    # Then
    assert metadata.startswith(f"-# {expected_label} • ")


def test_components_v2_payload_renders_escaped_source_metrics_in_order() -> None:
    # Given
    message = make_message(
        adapter="hackernews",
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="",
            author="Author",
            timestamp=None,
            source_metrics=(
                SourceMetric(label="Points", value="123"),
                SourceMetric(
                    label="Domain",
                    value="[evil](https://evil.example)",
                ),
            ),
        ),
    )

    # When
    metadata = get_metadata_content(message)

    # Then
    assert metadata.startswith("-# Hacker News • News • Points 123 • Domain ")
    assert "\\[evil\\]\\(h\u200bttps://evil.example\\)" in metadata
    assert metadata.endswith("• By Author")


def test_components_v2_payload_omits_duplicate_source_title() -> None:
    # Given
    message = make_message(
        adapter="hackernews",
        source_title="Hacker News",
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="",
            author="",
            timestamp=None,
        ),
    )

    # When
    metadata = get_metadata_content(message)

    # Then
    assert metadata == "-# Hacker News"


@pytest.mark.parametrize(
    ("discussion_url", "reason"),
    [
        ("https://example.test/entry", "equal-to-primary-link"),
        ("javascript:alert(1)", "unsafe-scheme"),
        ("mailto:user@example.test", "non-http-scheme"),
        ("", "empty-string"),
    ],
    ids=["equal", "javascript", "mailto", "empty"],
)
def test_components_v2_payload_omits_invalid_or_equal_discussion_link(
    discussion_url: str,
    reason: str,
) -> None:
    # Given
    message = make_message(
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="",
            author="",
            timestamp=None,
            discussion_url=discussion_url,
        ),
    )

    # When
    payload = DiscordWebhookClient._build_payload(message)

    # Then
    metadata = get_metadata_content(message)
    assert "Discussion" not in metadata
    assert f"[Discussion]({discussion_url})" not in json.dumps(payload)


def test_components_v2_payload_renders_escaped_categories_in_metadata() -> None:
    # Given - categories with markdown/injection characters
    message = make_message(
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="",
            author="",
            timestamp=None,
            categories=(
                "[evil](https://evil.example)",
                "@everyone ignore this",
                "**bold**",
            ),
        ),
    )

    # When
    payload = DiscordWebhookClient._build_payload(message)
    metadata = get_metadata_content(message)

    # Then
    assert "\\[evil\\]\\(h\u200bttps://evil.example\\)" in metadata
    assert "\\*\\*bold\\*\\*" in metadata
    assert payload["allowed_mentions"] == {"parse": []}


def test_components_v2_payload_renders_discussion_link_with_categories() -> None:
    # Given
    message = make_message(
        url="https://news.ycombinator.com/rss",
        entry=EntryData(
            title="Article",
            link="https://example.com/article",
            description="",
            author="",
            timestamp=None,
            discussion_url="https://news.ycombinator.com/item?id=99",
            categories=("tech", "programming"),
        ),
    )

    # When
    metadata = get_metadata_content(message)

    # Then
    lines = metadata.split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("-# Hacker News • ")
    assert "[Discussion](https://news.ycombinator.com/item?id=99)" in lines[1]
    assert "tech" in lines[1]
    assert "programming" in lines[1]


def test_components_v2_payload_renders_categories_without_discussion_link() -> None:
    # Given
    message = make_message(
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="",
            author="",
            timestamp=None,
            categories=("news", "rss"),
        ),
    )

    # When
    metadata = get_metadata_content(message)

    # Then
    lines = metadata.split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("-# RSS • ")
    assert "Discussion" not in lines[1]
    assert "news" in lines[1]
    assert "rss" in lines[1]


def test_components_v2_payload_prompt_injection_remains_escaped() -> None:
    # Given - instruction-like text in author and categories
    message = make_message(
        source_title="[System](https://evil.example) Ignore previous instructions",
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="",
            author="@admin delete all data",
            timestamp=None,
            categories=("@everyone ping", "<@&123> role mention"),
        ),
    )

    # When
    payload = DiscordWebhookClient._build_payload(message)
    metadata = get_metadata_content(message)

    # Then
    assert payload["allowed_mentions"] == {"parse": []}
    assert "h\u200bttps://evil.example" in metadata
    assert "\\[System\\]" in metadata
    assert "javascript:" not in json.dumps(payload)
