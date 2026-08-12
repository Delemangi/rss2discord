from rss2discord.adapters.reddit import RedditAdapter
from rss2discord.models import EntryData
from tests.discord_components_helpers import (
    get_all_components,
    get_metadata_content,
    get_text_display_contents,
    make_message,
)


def test_reddit_image_post_payload_links_discussion_and_renders_thumbnail() -> None:
    # Given
    discussion_url = "https://www.reddit.com/r/python/comments/abc123/example/"
    image_url = "https://i.redd.it/example.jpg"
    entry = RedditAdapter().adapt(
        {"content": [{"value": f'<a href="{image_url}">[link]</a>'}]},
        EntryData(
            title="Reddit post",
            link=discussion_url,
            description="Post body",
            author="/u/alice",
            timestamp=None,
        ),
    )
    message = make_message(adapter="reddit", entry=entry, source_title="r/Python")

    # When
    heading = get_text_display_contents(message)[0]
    thumbnails = [
        component
        for component in get_all_components(message)
        if component.get("type") == 11
    ]

    # Then
    assert discussion_url in heading
    assert thumbnails == [
        {
            "description": "Reddit post",
            "media": {"url": image_url},
            "type": 11,
        },
    ]
    assert "Discussion" not in get_metadata_content(message)
