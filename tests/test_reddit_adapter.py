from rss2discord.adapters.reddit import RedditAdapter
from rss2discord.models import EntryData


def make_entry() -> EntryData:
    return EntryData(
        title="Reddit post",
        link="https://www.reddit.com/r/python/comments/abc123/example/",
        description="Post body",
        author="/u/alice",
        timestamp="2026-07-21T09:00:00+00:00",
        categories=("Python",),
    )


def test_reddit_adapter_separates_outbound_and_discussion_links() -> None:
    # Given
    entry = {
        "content": [
            {
                "value": (
                    '<a href="https://example.test/article">[link]</a> '
                    '<a href="https://www.reddit.com/r/python/comments/abc123/">'
                    "[comments]</a>"
                ),
            },
        ],
    }
    adapter = RedditAdapter()

    # When
    result = adapter.adapt(entry, make_entry())

    # Then
    assert result.link == "https://example.test/article"
    assert result.discussion_url == make_entry().link
    assert result.author == "alice"


def test_reddit_adapter_keeps_self_post_link_without_duplicate_discussion() -> None:
    # Given
    baseline = make_entry()
    entry = {
        "content": [
            {"value": f'<a href="{baseline.link}">[link]</a>'},
        ],
    }
    adapter = RedditAdapter()

    # When
    result = adapter.adapt(entry, baseline)

    # Then
    assert result.link == baseline.link
    assert result.discussion_url is None
    assert result.author == "alice"


def test_reddit_adapter_preserves_entry_without_structured_content() -> None:
    # Given
    baseline = make_entry()
    adapter = RedditAdapter()

    # When
    result = adapter.adapt({}, baseline)

    # Then
    assert result.link == baseline.link
    assert result.discussion_url is None
    assert result.author == "alice"
