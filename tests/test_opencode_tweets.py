"""OpenCode tweets strategy tests."""

import feedparser
import pytest

from rss2discord.providers.opencode_tweets.strategy import OpenCodeTweetsStrategy

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>OpenCode / @opencode</title>
    <item>
      <title>Original tweet</title>
      <link>https://nitter.kareem.one/opencode/status/111#m</link>
      <guid>111</guid>
      <pubDate>Wed, 18 Nov 2026 17:28:40 GMT</pubDate>
      <description><![CDATA[Hello <img src="https://pbs.twimg.com/media/x.jpg" />]]></description>
    </item>
    <item>
      <title>R to @opencode: reply</title>
      <link>https://nitter.kareem.one/opencode/status/222#m</link>
      <guid>222</guid>
      <pubDate>Wed, 18 Nov 2026 17:28:41 GMT</pubDate>
      <description>Reply</description>
    </item>
    <item>
      <title>RT by @opencode: retweet</title>
      <link>https://nitter.kareem.one/opencode/status/333#m</link>
      <guid>333</guid>
      <pubDate>Wed, 18 Nov 2026 17:28:42 GMT</pubDate>
      <description>Retweet</description>
    </item>
    <item>
      <title>Pinned: pinned tweet</title>
      <link>https://nitter.kareem.one/opencode/status/444#m</link>
      <guid>444</guid>
      <pubDate>Wed, 18 Nov 2026 17:28:43 GMT</pubDate>
      <description>Pinned</description>
    </item>
  </channel>
</rss>
"""


def test_opencode_tweets_keeps_only_original_tweets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    strategy = OpenCodeTweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, title = strategy.fetch_entries("https://nitter.kareem.one/opencode/rss")

    # Then
    assert title == "OpenCode / @opencode"
    assert [strategy.get_entry_id(entry) for entry in filtered] == ["111"]
    data = strategy.get_entry_data(filtered[0])
    assert data.title == "Original tweet"
    assert data.link == "https://x.com/opencode/status/111"
    assert data.image_url == "https://pbs.twimg.com/media/x.jpg"
    assert strategy.seed_existing_on_first_fetch is True


def test_opencode_tweets_keeps_link_when_id_is_not_numeric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    entries[0]["id"] = "https://nitter.kareem.one/opencode/status/111#m"
    strategy = OpenCodeTweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries("https://nitter.kareem.one/opencode/rss")

    # Then
    assert strategy.get_entry_data(filtered[0]).link == (
        "https://nitter.kareem.one/opencode/status/111#m"
    )
