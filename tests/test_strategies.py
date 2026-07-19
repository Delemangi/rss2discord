import feedparser

from strategies import RSSStrategy, XenForoStrategy


def test_rss_strategy_uses_stable_native_identity() -> None:
    # Given
    strategy = RSSStrategy()
    identified_entry = feedparser.FeedParserDict({"id": "guid-1"})
    linked_entry = feedparser.FeedParserDict({"link": "https://example.test/1"})
    unidentified_entry = feedparser.FeedParserDict({"title": "No stable identity"})

    # When / Then
    assert strategy.get_entry_id(identified_entry) == "guid-1"
    assert strategy.get_entry_id(linked_entry) == "https://example.test/1"
    assert strategy.get_entry_id(unidentified_entry) is None


def test_xenforo_strategy_requires_post_id() -> None:
    # Given
    strategy = XenForoStrategy()

    # When / Then
    assert strategy.get_entry_id({"id": 42}) == "42"
    assert strategy.get_entry_id({"content": "No stable identity"}) is None


def test_rss_strategy_does_not_invent_missing_timestamp() -> None:
    # Given
    strategy = RSSStrategy()
    entry = feedparser.FeedParserDict({"published": "not-a-date"})

    # When / Then
    assert strategy._get_timestamp(entry) is None


def test_xenforo_strategy_does_not_invent_missing_timestamp() -> None:
    # Given
    strategy = XenForoStrategy()

    # When / Then
    assert strategy._get_timestamp({}) is None
