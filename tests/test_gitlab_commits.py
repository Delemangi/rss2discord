from pathlib import Path
from typing import Final

import feedparser
import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.discord.source_labels import source_label
from rss2discord.transports import RSSStrategy

FIXTURE: Final = (
    Path(__file__).parent / "fixtures" / "gitlab" / "horizon-application-main.atom"
)


def test_gitlab_atom_entries_extract_stable_ids_content_and_dates() -> None:
    # Given
    feed = feedparser.parse(FIXTURE.read_bytes())
    strategy = RSSStrategy()

    # When
    extracted = tuple(
        (strategy.get_entry_id(entry), strategy.get_entry_data(entry))
        for entry in feed.entries
    )

    # Then
    assert [entry_id for entry_id, _data in extracted] == [
        (
            "https://gitlab.finki.ukim.mk/wp/horizon-application/-/commit/"
            "1111111111111111111111111111111111111111"
        ),
        (
            "https://gitlab.finki.ukim.mk/wp/horizon-application/-/commit/"
            "2222222222222222222222222222222222222222"
        ),
    ]
    assert extracted[0][1].description == (
        "Correct the worker import and keep startup & shutdown symmetric."
    )
    assert extracted[0][1].link.endswith("/1111111111111111111111111111111111111111")
    assert extracted[0][1].author == "Horizon Maintainer (maintainer@example.test)"
    assert extracted[0][1].timestamp == "2026-09-14T08:15:00+00:00"
    assert extracted[1][1].timestamp == "2026-09-14T09:30:00+00:00"


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.finki.ukim.mk/wp/horizon-application/-/commits/main.atom",
        "https://gitlab.finki.ukim.mk/wp/horizon-application/-/commits/develop.atom",
        "https://example.test/group/project/-/commits/release%2F1.0.atom",
        "https://gitlab.finki.ukim.mk/wp/horizon-application/-/commits/main?format=atom&ref=main",
    ],
)
def test_gitlab_commit_feed_shape_gets_gitlab_label(url: str) -> None:
    feed = FeedConfig(
        id="gitlab",
        url=url,
        webhook="https://discord.test/webhook",
    )

    assert source_label(feed) == "GitLab"


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.finki.ukim.mk/wp/horizon-application/-/issues.atom",
        "https://gitlab.finki.ukim.mk/-/commits/main.atom",
        "https://gitlab.finki.ukim.mk/wp/horizon-application/commits/main.atom",
        "https://gitlab.finki.ukim.mk/wp/horizon-application/-/commits/main?format=rss",
        "https://gitlab.finki.ukim.mk/wp/horizon-application/-/commits/main?note=atom",
        "https://gitlab.finki.ukim.mk/wp/horizon-application/-/commits?format=atom",
        "https://gitlab.finki.ukim.mk/wp/horizon-application/-/issues/main?format=atom",
    ],
)
def test_unrelated_rss_paths_are_not_labeled_gitlab(url: str) -> None:
    feed = FeedConfig(
        id="feed",
        url=url,
        webhook="https://discord.test/webhook",
    )

    assert source_label(feed) == "RSS"
