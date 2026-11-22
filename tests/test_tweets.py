"""Generic tweets strategy tests."""

from urllib.parse import urlsplit

import feedparser
import pytest

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.tweets.strategy import (
    TWEET_TITLE_LIMIT,
    TWEETS_FALLBACK_LABEL,
    TWEETS_MEDIA_TITLE,
    TweetsStrategy,
    _candidate_urls,
)
from rss2discord.retries import FeedFetchInterruptedError

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
    <item>
      <title>R to the moon</title>
      <link>https://nitter.kareem.one/opencode/status/555#m</link>
      <guid>555</guid>
      <pubDate>Wed, 18 Nov 2026 17:28:44 GMT</pubDate>
      <description>Not a reply</description>
    </item>
    <item>
      <title>RT by the way</title>
      <link>https://nitter.kareem.one/opencode/status/666#m</link>
      <guid>666</guid>
      <pubDate>Wed, 18 Nov 2026 17:28:45 GMT</pubDate>
      <description>Not a retweet</description>
    </item>
    <item>
      <title>Another account tweet</title>
      <link>https://nitter.example/other/status/777#m</link>
      <guid>777</guid>
      <pubDate>Wed, 18 Nov 2026 17:28:46 GMT</pubDate>
      <description>Other</description>
    </item>
    <item>
      <title>No handle in link</title>
      <link>https://nitter.example/i/web/status/888</link>
      <guid>888</guid>
      <pubDate>Wed, 18 Nov 2026 17:28:47 GMT</pubDate>
      <description>Unusual link</description>
    </item>
  </channel>
</rss>
"""


def _strategy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    title: str = "OpenCode / @opencode",
) -> TweetsStrategy:
    entries = feedparser.parse(FEED).entries
    strategy = TweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, title),
    )
    return strategy


def test_tweets_keeps_only_original_tweets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    strategy = _strategy(monkeypatch)

    # When
    filtered, title = strategy.fetch_entries("https://nitter.kareem.one/opencode/rss")

    # Then
    assert title == "OpenCode / @opencode"
    assert [strategy.get_entry_id(entry) for entry in filtered] == [
        "111",
        "555",
        "666",
        "777",
        "888",
    ]
    assert strategy.seed_existing_on_first_fetch is True


def test_tweets_can_include_retweets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    strategy = TweetsStrategy(include_retweets=True)
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries("https://nitter.kareem.one/opencode/rss")

    # Then
    assert [strategy.get_entry_id(entry) for entry in filtered] == [
        "111",
        "333",
        "555",
        "666",
        "777",
        "888",
    ]


def test_tweets_keeps_public_nitter_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    strategy = _strategy(monkeypatch)
    filtered, _title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # When / Then
    first = strategy.get_entry_data(filtered[0])
    assert first.link == "https://nitter.kareem.one/opencode/status/111"
    assert first.image_url == "https://pbs.twimg.com/media/x.jpg"
    assert first.image_urls == ("https://pbs.twimg.com/media/x.jpg",)
    assert strategy.get_entry_data(filtered[2]).link == (
        "https://nitter.kareem.one/opencode/status/666"
    )
    assert strategy.get_entry_data(filtered[3]).link == (
        "https://nitter.example/other/status/777"
    )
    assert strategy.get_entry_data(filtered[4]).link == (
        "https://nitter.example/i/web/status/888"
    )


def test_tweets_upgrades_nitter_image_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    entries[0]["summary"] = (
        '<p>Photo</p><img src="http://nitter.kareem.one/pic/media%2Fabc.jpg" />'
    )
    strategy = TweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    assert strategy.get_entry_data(filtered[0]).image_url == (
        "https://pbs.twimg.com/media/abc.jpg"
    )
    assert strategy.get_entry_data(filtered[0]).image_urls == (
        "https://pbs.twimg.com/media/abc.jpg",
    )


def test_tweets_prefers_attached_media_over_link_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    entries[0]["summary"] = (
        '<img src="http://nitter.kareem.one/pic/card_img%2F1%2Fcard.jpg" />'
        '<img src="http://nitter.kareem.one/pic/media%2Fphoto.jpg" />'
    )
    strategy = TweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    assert strategy.get_entry_data(filtered[0]).image_url == (
        "https://pbs.twimg.com/media/photo.jpg"
    )
    assert strategy.get_entry_data(filtered[0]).image_urls == (
        "https://pbs.twimg.com/media/photo.jpg",
    )


def test_tweets_exposes_every_attached_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    entries[0]["summary"] = (
        '<img src="http://nitter.kareem.one/pic/media%2Ffirst.jpg" />'
        '<img src="http://nitter.kareem.one/pic/media%2Fsecond.jpg" />'
    )
    strategy = TweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    data = strategy.get_entry_data(filtered[0])
    assert data.image_url == "https://pbs.twimg.com/media/first.jpg"
    assert data.image_urls == (
        "https://pbs.twimg.com/media/first.jpg",
        "https://pbs.twimg.com/media/second.jpg",
    )


def test_tweets_drops_description_that_repeats_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    entries[0]["title"] = "Identical text"
    entries[0]["summary"] = "<p>Identical text</p>"
    strategy = TweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    data = strategy.get_entry_data(filtered[0])
    assert data.title == "Identical text"
    assert data.description == ""


def test_tweets_clips_long_title_and_keeps_full_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    long_text = " ".join(["word"] * 40)
    entries = feedparser.parse(FEED).entries
    entries[0]["title"] = long_text
    entries[0]["summary"] = f"<p>{long_text}</p>"
    strategy = TweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    data = strategy.get_entry_data(filtered[0])
    assert data.title.endswith("…")
    assert len(data.title) <= TWEET_TITLE_LIMIT
    assert data.description == long_text


def test_tweets_uses_link_card_without_repeating_tweet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    entries[0]["title"] = "Check this"
    entries[0]["summary"] = (
        "<p>Check this</p>"
        "<hr />"
        "<b>Link</b><br />"
        "<b>Card title</b>"
        "<p>Card description</p>"
        '<footer>— <a href="http://nitter.kareem.one/opencode/status/111#m">'
        "http://nitter.kareem.one/opencode/status/111#m</a></footer>"
    )
    strategy = TweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    data = strategy.get_entry_data(filtered[0])
    assert data.title == "Check this"
    assert data.description == "Card title\n\nCard description"
    assert "Check this" not in data.description
    assert "Link" not in data.description
    assert "status" not in data.description


def test_tweets_cleans_nitter_labels_and_status_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    entries[0]["summary"] = (
        "<p>Caption</p>"
        '<a href="http://nitter.kareem.one/opencode/status/111#m">Video</a>'
        '<img src="http://nitter.kareem.one/pic/media%2Fphoto.jpg" />'
        " http://nitter.kareem.one/opencode/status/111#m"
    )
    strategy = TweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    description = strategy.get_entry_data(filtered[0]).description
    assert description == "Caption"
    assert "Video" not in description
    assert "status" not in description


def test_tweets_repairs_nitter_separator_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    entries[0]["summary"] = "<p>1M context \ufffd Multimodal \ufffd ZDR</p>"
    strategy = TweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    description = strategy.get_entry_data(filtered[0]).description
    assert description == "1M context · Multimodal · ZDR"
    assert "\ufffd" not in description


def test_tweets_uses_media_fallback_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    entries[0]["title"] = ""
    entries[0]["summary"] = (
        '<img src="http://nitter.kareem.one/pic/media%2Fphoto.jpg" />'
    )
    strategy = TweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    assert strategy.get_entry_data(filtered[0]).title == TWEETS_MEDIA_TITLE


def test_tweets_upgrades_http_links_and_drops_media_fragments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    entries[0]["link"] = "http://nitter.kareem.one/opencode/status/111#m"
    strategy = TweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    assert strategy.get_entry_data(filtered[0]).link == (
        "https://nitter.kareem.one/opencode/status/111"
    )


def test_tweets_keeps_link_when_id_is_not_numeric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    entries[0]["id"] = "https://nitter.kareem.one/opencode/status/111#m"
    strategy = TweetsStrategy()
    monkeypatch.setattr(
        strategy._rss,
        "fetch_entries",
        lambda _url: (entries, "OpenCode / @opencode"),
    )

    # When
    filtered, _title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    assert strategy.get_entry_data(filtered[0]).link == (
        "https://nitter.kareem.one/opencode/status/111"
    )


def test_tweets_uses_fallback_label_when_feed_title_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    strategy = _strategy(monkeypatch, title="")

    # When
    _filtered, title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    assert title == TWEETS_FALLBACK_LABEL


def test_tweets_fails_over_to_another_nitter_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    entries = feedparser.parse(FEED).entries
    strategy = TweetsStrategy()
    calls: list[str] = []

    def fetch(url: str) -> tuple[list[object], str]:
        calls.append(url)
        if len(calls) == 1:
            raise FeedFetchError(
                "RSS",
                "HTTPError",
                status_code=429,
                retryable=True,
            )
        return entries, "OpenCode / @opencode"

    monkeypatch.setattr(strategy._rss, "fetch_entries", fetch)

    # When
    filtered, title = strategy.fetch_entries(
        "https://nitter.kareem.one/opencode/rss",
    )

    # Then
    assert title == "OpenCode / @opencode"
    assert len(calls) == 2
    assert calls[0] == "https://nitter.kareem.one/opencode/rss"
    assert calls[1] != calls[0]
    assert urlsplit(calls[1]).path == "/opencode/rss"
    assert [strategy.get_entry_id(entry) for entry in filtered] == [
        "111",
        "555",
        "666",
        "777",
        "888",
    ]


def test_tweets_raises_the_last_error_when_every_instance_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    strategy = TweetsStrategy()
    errors: list[FeedFetchError] = []

    def fetch(_url: str) -> tuple[list[object], str]:
        error = FeedFetchError("RSS", "HTTPError", status_code=503, retryable=True)
        errors.append(error)
        raise error

    monkeypatch.setattr(strategy._rss, "fetch_entries", fetch)

    # When / Then
    with pytest.raises(FeedFetchError) as caught:
        strategy.fetch_entries("https://nitter.kareem.one/opencode/rss")
    assert caught.value is errors[-1]
    assert len(errors) > 1


def test_tweets_does_not_fail_over_on_non_retryable_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    strategy = TweetsStrategy()
    calls: list[str] = []
    error = FeedFetchError("RSS", "HTTPError", status_code=404)

    def fetch(url: str) -> tuple[list[object], str]:
        calls.append(url)
        raise error

    monkeypatch.setattr(strategy._rss, "fetch_entries", fetch)

    # When / Then
    with pytest.raises(FeedFetchError) as caught:
        strategy.fetch_entries("https://nitter.kareem.one/opencode/rss")
    assert caught.value is error
    assert calls == ["https://nitter.kareem.one/opencode/rss"]


def test_candidate_urls_keeps_malformed_port_url_unchanged() -> None:
    url = "https://nitter.kareem.one:abc/opencode/rss"

    assert _candidate_urls(url) == (url,)


def test_tweets_propagates_shutdown_without_failing_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    strategy = TweetsStrategy()
    calls: list[str] = []

    def fetch(url: str) -> tuple[list[object], str]:
        calls.append(url)
        raise FeedFetchInterruptedError

    monkeypatch.setattr(strategy._rss, "fetch_entries", fetch)

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        strategy.fetch_entries("https://nitter.kareem.one/opencode/rss")
    assert len(calls) == 1
