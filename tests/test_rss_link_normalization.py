import feedparser
import pytest

from rss2discord.transports import RSSStrategy
from tests.discord_components_helpers import get_text_display_contents, make_message


@pytest.mark.parametrize(
    ("raw_link", "expected_link"),
    [
        ("  https://example.test/article  ", "https://example.test/article"),
        ("https://user:secret@example.test/article", ""),
        ("javascript:alert(1)", ""),
        ("/relative/article", ""),
    ],
)
def test_rss_card_normalizes_primary_link(
    raw_link: str,
    expected_link: str,
) -> None:
    # Given
    entry = feedparser.FeedParserDict(
        {"title": "Article", "link": raw_link},
    )

    # When
    entry_data = RSSStrategy().get_entry_data(entry)
    heading = get_text_display_contents(make_message(entry=entry_data))[0]

    # Then
    assert entry_data.link == expected_link
    if expected_link:
        assert expected_link in heading
    else:
        assert heading == "## Article"


@pytest.mark.parametrize(
    "raw_discussion_url",
    [
        "javascript:alert(1)",
        "/relative/comments",
        "https://example.test/comments\nunsafe",
    ],
)
def test_rss_card_omits_unsafe_discussion_link(raw_discussion_url: str) -> None:
    # Given
    entry = feedparser.FeedParserDict(
        {
            "title": "Article",
            "link": "https://example.test/article",
            "comments": raw_discussion_url,
        },
    )

    # When
    entry_data = RSSStrategy().get_entry_data(entry)

    # Then
    assert entry_data.discussion_url is None
