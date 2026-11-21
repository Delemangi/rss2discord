"""Exercise Reddit pagination, filtering, mapping, and app delivery."""

import json
from pathlib import Path

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.providers.arctic_shift.client import ArcticShiftClient
from rss2discord.providers.reddit.client import RedditClient, parse_reddit_listing
from rss2discord.providers.reddit.models import (
    RedditGalleryData,
    RedditGalleryItem,
    RedditMedia,
    RedditMediaSource,
    RedditPage,
    RedditPost,
)
from rss2discord.providers.reddit.scope import RedditScope
from rss2discord.providers.reddit.strategy import RedditStrategy
from tests.app_helpers import FakeSender
from tests.reddit_helpers import (
    FakeListingClient,
    FakeResponse,
    FakeSession,
    fixture_bytes,
    make_reddit_client,
)


def fixture_page(name: str) -> RedditPage:
    return parse_reddit_listing(fixture_bytes(name))


def arctic_fixture_bytes(name: str) -> bytes:
    return (Path(__file__).parent / "fixtures" / "arctic_shift" / name).read_bytes()


def make_post(name: str, **overrides: object) -> RedditPost:
    values: dict[str, object] = {
        "name": name,
        "id": name.removeprefix("t3_"),
        "title": name,
        "author": "author",
        "created_utc": 1_700_000_000.0,
        "permalink": f"/r/python/comments/{name}/post/",
    }
    values.update(overrides)
    return RedditPost.model_validate(values)


def make_page(
    prefix: str,
    after: str | None,
    *,
    count: int = 1,
) -> RedditPage:
    return RedditPage(
        posts=tuple(make_post(f"t3_{prefix}{index}") for index in range(count)),
        after=after,
    )


def test_fetch_walks_pages_oldest_first_and_tracks_count_and_cursor() -> None:
    # Given
    page_one = fixture_page("listing_page_1.json")
    page_two = fixture_page("listing_page_2.json")
    client = FakeListingClient([page_one, page_two])
    strategy = RedditStrategy(client=client)

    # When
    entries, source_title = strategy.fetch_entries(
        "https://www.reddit.com/r/python/new",
    )

    # Then
    assert [entry.name for entry in entries] == [
        "t3_image2",
        "t3_image1",
        "t3_gallery1",
        "t3_self1",
        "t3_link1",
    ]
    assert source_title == "r/python"
    assert client.calls == [
        (RedditScope("python", "new"), None, 0),
        (RedditScope("python", "new"), "t3_cursor2", 3),
    ]


def test_fetch_stops_at_the_three_page_cap() -> None:
    # Given
    pages = [
        make_page("first", "cursor-1"),
        make_page("second", "cursor-2"),
        make_page("third", "cursor-3"),
        make_page("fourth", None),
    ]
    client = FakeListingClient(pages)
    strategy = RedditStrategy(client=client)

    # When
    entries, _ = strategy.fetch_entries("https://www.reddit.com/r/python/new")

    # Then
    assert [entry.name for entry in entries] == [
        "t3_third0",
        "t3_second0",
        "t3_first0",
    ]
    assert len(client.calls) == 3


def test_fetch_stops_at_the_three_hundred_post_cap() -> None:
    # Given
    client = FakeListingClient([make_page("page", "cursor-1", count=300)])
    strategy = RedditStrategy(client=client)

    # When
    entries, _ = strategy.fetch_entries("https://www.reddit.com/r/python/new")

    # Then
    assert len(entries) == 300
    assert len(client.calls) == 1


def test_fetch_stops_on_a_repeated_after_cursor() -> None:
    # Given
    client = FakeListingClient(
        [
            make_page("first", "same-cursor"),
            make_page("second", "same-cursor"),
            make_page("third", None),
        ],
    )
    strategy = RedditStrategy(client=client)

    # When
    entries, _ = strategy.fetch_entries("https://www.reddit.com/r/python/new")

    # Then
    assert [entry.name for entry in entries] == ["t3_second0", "t3_first0"]
    assert [call[1] for call in client.calls] == [None, "same-cursor"]


def test_fetch_dedupes_posts_across_pages() -> None:
    # Given a boundary post repeated on the next page
    shared = make_post("t3_shared")
    client = FakeListingClient(
        [
            RedditPage(
                posts=(make_post("t3_newer"), shared),
                after="cursor-1",
            ),
            RedditPage(
                posts=(shared, make_post("t3_older")),
                after=None,
            ),
        ],
    )
    strategy = RedditStrategy(client=client)

    # When
    entries, _ = strategy.fetch_entries("https://www.reddit.com/r/python/new")

    # Then
    assert [entry.name for entry in entries] == [
        "t3_older",
        "t3_shared",
        "t3_newer",
    ]
    assert len(client.calls) == 2


def test_fetch_returns_empty_listing() -> None:
    # Given
    client = FakeListingClient([fixture_page("listing_empty.json")])
    strategy = RedditStrategy(client=client)

    # When
    entries, source_title = strategy.fetch_entries(
        "https://www.reddit.com/r/python/new",
    )

    # Then
    assert entries == []
    assert source_title == "r/python"


def test_fetch_skips_stickied_nsfw_and_spoiler_posts() -> None:
    # Given
    page = fixture_page("listing_page_1.json")
    client = FakeListingClient(
        [RedditPage(posts=page.posts, after=None)],
    )
    strategy = RedditStrategy(client=client)

    # When
    entries, _ = strategy.fetch_entries("https://www.reddit.com/r/python/new")

    # Then
    assert {entry.name for entry in entries} == {
        "t3_link1",
        "t3_self1",
        "t3_gallery1",
    }


def test_fetch_skips_pinned_posts() -> None:
    # Given
    page = RedditPage(
        posts=(
            make_post("t3_normal"),
            make_post("t3_pinned", pinned=True),
        ),
        after=None,
    )
    strategy = RedditStrategy(client=FakeListingClient([page]))

    # When
    entries, _ = strategy.fetch_entries("https://www.reddit.com/r/python/new")

    # Then
    assert [entry.name for entry in entries] == ["t3_normal"]


def test_fetch_skips_removed_and_deleted_posts() -> None:
    # Given
    page = RedditPage(
        posts=(
            make_post("t3_normal"),
            make_post(
                "t3_removed_title",
                title="[ Removed by moderator ]",
                permalink="/r/python/comments/removed/removed_by_moderator/",
            ),
            make_post(
                "t3_removed_self",
                is_self=True,
                selftext="[removed]",
            ),
            make_post("t3_deleted_author", author="[deleted]"),
        ),
        after=None,
    )
    strategy = RedditStrategy(
        client=FakeListingClient([page]),
        filter_removed=True,
    )

    # When
    entries, _ = strategy.fetch_entries("https://www.reddit.com/r/python/new")

    # Then
    assert [entry.name for entry in entries] == ["t3_normal"]


def test_arctic_shift_filters_removed_posts_and_maps_media() -> None:
    # Given
    session = FakeSession(
        [FakeResponse(200, arctic_fixture_bytes("posts-page.json"))],
    )
    strategy = RedditStrategy(
        client=ArcticShiftClient(session=session),
        show_engagement=False,
        filter_removed=True,
    )

    # When
    entries, source_title = strategy.fetch_entries(
        "https://www.reddit.com/r/python/new",
    )

    # Then
    assert source_title == "r/python"
    assert [entry.name for entry in entries] == [
        "t3_link1",
        "t3_self1",
        "t3_image1",
        "t3_gallery1",
    ]
    by_name = {
        entry.name: strategy.get_entry_data(entry)
        for entry in entries
        if entry.name is not None
    }
    assert by_name["t3_link1"].link == "https://example.com/article"
    assert by_name["t3_link1"].discussion_url == (
        "https://www.reddit.com/r/python/comments/link1/interesting_link/"
    )
    assert {
        metric.label: metric.value for metric in by_name["t3_link1"].source_metrics
    } == {"Domain": "example.com"}
    assert by_name["t3_image1"].image_url == "https://i.redd.it/image1.jpg"
    assert by_name["t3_gallery1"].image_urls == (
        "https://i.redd.it/m1.png?width=640&format=png",
        "https://i.redd.it/m2.png?width=640&format=png",
    )
    assert session.calls[0][2]["params"] == {
        "subreddit": "python",
        "limit": 100,
        "sort": "desc",
    }


def test_link_post_maps_outbound_link_and_discussion() -> None:
    # Given
    strategy = RedditStrategy(client=FakeListingClient([]))
    entry = fixture_page("listing_page_1.json").posts[0]

    # When
    data = strategy.get_entry_data(entry)

    # Then
    assert strategy.get_entry_id(entry) == "t3_link1"
    assert data.title == "Interesting link"
    assert data.link == "https://example.com/article"
    assert data.discussion_url == (
        "https://www.reddit.com/r/python/comments/link1/interesting_link/"
    )
    assert data.image_url is None
    assert data.image_urls == ()
    assert data.description == ""
    assert data.author == "alice"
    assert data.timestamp == "2023-11-14T22:18:20+00:00"
    assert data.categories == ("News",)
    assert {metric.label: metric.value for metric in data.source_metrics} == {
        "Score": "42",
        "Comments": "7",
        "Domain": "example.com",
    }


def test_self_post_maps_discussion_as_link_without_outbound() -> None:
    # Given
    strategy = RedditStrategy(client=FakeListingClient([]))
    entry = fixture_page("listing_page_1.json").posts[1]

    # When
    data = strategy.get_entry_data(entry)

    # Then
    assert data.link == (
        "https://www.reddit.com/r/python/comments/self1/ask_about_python/"
    )
    assert data.discussion_url is None
    assert data.image_url is None
    assert data.description == "Hello & welcome to the thread"
    assert {metric.label: metric.value for metric in data.source_metrics} == {
        "Score": "10",
        "Comments": "3",
    }


def test_gallery_maps_ordered_image_urls() -> None:
    # Given
    strategy = RedditStrategy(client=FakeListingClient([]))
    entry = fixture_page("listing_page_1.json").posts[2]

    # When
    data = strategy.get_entry_data(entry)

    # Then
    assert data.link == (
        "https://www.reddit.com/r/python/comments/gallery1/chart_gallery/"
    )
    assert data.discussion_url is None
    assert data.image_urls == (
        "https://i.redd.it/m1.png?width=640&format=png",
        "https://i.redd.it/m2.png?width=640&format=png",
    )
    assert data.image_url == data.image_urls[0]
    assert data.description == ""


def test_image_post_prefers_unescaped_preview_url() -> None:
    # Given
    strategy = RedditStrategy(client=FakeListingClient([]))
    entry = fixture_page("listing_page_2.json").posts[0]

    # When
    data = strategy.get_entry_data(entry)

    # Then
    assert data.link == ("https://www.reddit.com/r/python/comments/image1/screenshot/")
    assert data.discussion_url is None
    assert data.image_url == (
        "https://preview.redd.it/image1.jpg?width=640&format=pjpg"
    )
    assert data.image_urls == (data.image_url,)


def test_image_post_falls_back_to_a_safe_https_thumbnail() -> None:
    # Given
    strategy = RedditStrategy(client=FakeListingClient([]))
    entry = fixture_page("listing_page_2.json").posts[1]

    # When
    data = strategy.get_entry_data(entry)

    # Then
    assert data.image_url == "https://b.thumbs.redditmedia.com/fallback2.jpg"
    assert data.image_urls == (data.image_url,)


@pytest.mark.parametrize(
    "thumbnail",
    ["self", "default", "nsfw", "spoiler", "http://example.test/thumb.jpg", "junk"],
)
def test_image_post_ignores_unsafe_thumbnail_sentinels(thumbnail: str) -> None:
    # Given
    strategy = RedditStrategy(client=FakeListingClient([]))
    entry = make_post("t3_image", post_hint="image", thumbnail=thumbnail)

    # When
    data = strategy.get_entry_data(entry)

    # Then
    assert data.image_url is None
    assert data.image_urls == ()


def test_image_post_falls_back_to_a_direct_i_redd_it_url() -> None:
    # Given
    strategy = RedditStrategy(client=FakeListingClient([]))
    entry = make_post(
        "t3_image",
        post_hint="image",
        thumbnail="self",
        url_overridden_by_dest="https://i.redd.it/direct.jpg",
    )

    # When
    data = strategy.get_entry_data(entry)

    # Then
    assert data.image_url == "https://i.redd.it/direct.jpg"
    assert data.image_urls == (data.image_url,)


def test_image_post_direct_fallback_rejects_other_hosts() -> None:
    # Given
    strategy = RedditStrategy(client=FakeListingClient([]))
    entry = make_post(
        "t3_image",
        post_hint="image",
        thumbnail="self",
        url_overridden_by_dest="https://example.com/image.jpg",
    )

    # When
    data = strategy.get_entry_data(entry)

    # Then
    assert data.image_url is None
    assert data.image_urls == ()


def test_arctic_shift_metrics_omit_archive_engagement_counts() -> None:
    # Given
    strategy = RedditStrategy(
        client=FakeListingClient([]),
        show_engagement=False,
    )
    entry = make_post(
        "t3_link",
        domain="example.com",
        score=1,
        num_comments=0,
    )

    # When
    data = strategy.get_entry_data(entry)

    # Then
    assert {metric.label: metric.value for metric in data.source_metrics} == {
        "Domain": "example.com",
    }


def test_gallery_omits_missing_or_odd_image_metadata() -> None:
    # Given
    strategy = RedditStrategy(client=FakeListingClient([]))
    entry = make_post(
        "t3_gallery",
        is_gallery=True,
        post_hint="image",
        gallery_data=RedditGalleryData(
            items=[RedditGalleryItem(media_id="missing")],
        ),
        media_metadata={
            "other": RedditMedia(
                status="failed",
                s=RedditMediaSource(u=None),
            ),
        },
    )

    # When
    data = strategy.get_entry_data(entry)

    # Then
    assert data.image_url is None
    assert data.image_urls == ()
    assert data.discussion_url is None


def test_malformed_listing_children_are_skipped_individually() -> None:
    # Given
    payload = {
        "kind": "Listing",
        "data": {
            "after": None,
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "name": "t3_valid",
                        "title": "Valid post",
                        "permalink": "/r/python/comments/valid/post/",
                    },
                },
                {"kind": "t3"},
                {"kind": "t3", "data": {"name": "t3_bad_title", "title": 123}},
                "not-a-child",
                {"kind": "t1", "data": {"name": "t1_comment", "title": "Comment"}},
            ],
        },
    }

    # When
    page = parse_reddit_listing(json.dumps(payload).encode())

    # Then
    assert [post.name for post in page.posts] == ["t3_valid"]


def test_strategy_passes_scope_and_page_params_to_the_real_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("REDDIT_CLIENT_ID", "fixture-id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "fixture-secret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "rss2discord-test/1.0")
    monkeypatch.delenv("REDDIT_ACCESS_TOKEN", raising=False)
    session = FakeSession(
        [
            FakeResponse(200, fixture_bytes("token_success.json")),
            FakeResponse(200, fixture_bytes("listing_empty.json")),
        ],
    )
    client: RedditClient = make_reddit_client(session)
    strategy = RedditStrategy(client=client)

    # When
    entries, _ = strategy.fetch_entries(
        "https://www.reddit.com/r/python/top?t=week",
    )

    # Then
    assert entries == []
    method, url, kwargs = session.calls[1]
    assert method == "GET"
    assert url == "https://oauth.reddit.com/r/python/top"
    assert kwargs["params"] == {"limit": 100, "raw_json": 1, "t": "week"}


def test_reddit_app_integration_seeds_then_delivers_one_new_post(
    tmp_path: Path,
) -> None:
    # Given
    feed = FeedConfig(
        id="reddit-python",
        name="r/python",
        url="https://www.reddit.com/r/python/new",
        webhook="https://discord.test/reddit",
        strategy="reddit",
    )
    sender = FakeSender([True])
    old_post = make_post("t3_old", title="Old post")
    new_post = make_post(
        "t3_new",
        title="New post",
        created_utc=1_700_000_100.0,
    )
    client = FakeListingClient(
        [RedditPage(posts=(old_post,), after=None)],
    )
    strategy = RedditStrategy(client=client)

    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(
            config=AppConfig(
                delay_between_posts=0,
                max_post_age_days=0,
                feeds=(feed,),
            ),
            store=store,
            sender=sender,
        )
        app._strategies["reddit"] = strategy

        # When
        app.process_feed(feed)
        seeded_messages = list(sender.messages)
        client.pages.append(
            RedditPage(posts=(new_post, old_post), after=None),
        )
        app.process_feed(feed)

        # Then
        assert store.is_feed_initialized(feed.id)
        assert store.has_delivered(feed.id, "t3_old")
        assert store.has_delivered(feed.id, "t3_new")

    assert seeded_messages == []
    assert [message.entry.title for message in sender.messages] == ["New post"]
