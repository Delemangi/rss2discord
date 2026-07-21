from typing import Final

import feedparser
import pytest

from rss2discord.models import EntryData
from rss2discord.transports import RSSStrategy
from tests.discord_components_helpers import get_metadata_content, make_message

GITHUB_RELEASE_ATOM: Final = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:media="http://search.yahoo.com/mrss/">
  <id>tag:github.com,2008:https://github.com/cli/cli/releases</id>
  <title>Release notes from cli</title>
  <updated>2026-07-02T21:36:38Z</updated>
  <entry>
    <id>tag:github.com,2008:Repository/212613049/v2.96.0</id>
    <updated>2026-07-02T21:36:38Z</updated>
    <link rel="alternate" type="text/html"
          href="https://github.com/cli/cli/releases/tag/v2.96.0" />
    <title>GitHub CLI 2.96.0</title>
    <content type="html">&lt;p&gt;Security &amp;amp; reliability fixes&lt;/p&gt;</content>
    <author><name>github-actions[bot]</name></author>
    <media:thumbnail height="30" width="30"
                     url="https://avatars.githubusercontent.com/u/65916846?v=4" />
  </entry>
</feed>
"""


@pytest.mark.parametrize(
    ("url", "expected_label"),
    [
        ("https://github.com/cli/cli/releases.atom", "GitHub"),
        ("https://GitHub.com/cli/cli/releases.atom?source=discord", "GitHub"),
        ("https://github.com/cli/cli/releases", "RSS"),
        ("https://github.com/cli/releases.atom", "RSS"),
        ("https://github.com.evil.test/cli/cli/releases.atom", "RSS"),
    ],
    ids=[
        "release-feed",
        "case-insensitive-host",
        "release-page",
        "missing-repository",
        "lookalike-host",
    ],
)
def test_components_v2_payload_identifies_github_release_feeds(
    url: str,
    expected_label: str,
) -> None:
    # Given
    message = make_message(
        url=url,
        entry=EntryData(
            title="Release",
            link="https://github.com/cli/cli/releases/tag/v2.96.0",
            description="",
            author="",
            timestamp=None,
        ),
    )

    # When
    metadata = get_metadata_content(message)

    # Then
    assert metadata.startswith(f"-# {expected_label} • ")


def test_rss_strategy_normalizes_github_release_atom_entry() -> None:
    # Given
    strategy = RSSStrategy()
    parsed_feed = feedparser.parse(GITHUB_RELEASE_ATOM)
    entry = parsed_feed.entries[0]

    # When
    entry_id = strategy.get_entry_id(entry)
    entry_data = strategy.get_entry_data(entry)

    # Then
    assert entry_id == "tag:github.com,2008:Repository/212613049/v2.96.0"
    assert entry_data.title == "GitHub CLI 2.96.0"
    assert entry_data.link == "https://github.com/cli/cli/releases/tag/v2.96.0"
    assert entry_data.description == "Security & reliability fixes"
    assert entry_data.author == "github-actions[bot]"
    assert entry_data.timestamp == "2026-07-02T21:36:38+00:00"
    assert entry_data.image_url == (
        "https://avatars.githubusercontent.com/u/65916846?v=4"
    )
