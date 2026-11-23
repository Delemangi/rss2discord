import json
from dataclasses import replace

import pytest

from rss2discord.discord.components import (
    MAX_THUMBNAIL_DESCRIPTION_CHARACTERS,
    JSONValue,
    build_components_v2_payload,
)
from tests.discord_components_helpers import (
    build_payload,
    get_all_components,
    get_container_children,
    get_media_galleries,
    get_media_gallery_items,
    get_metadata_content,
    get_text_display_contents,
    get_thumbnail_url,
    get_thumbnail_url_from_children,
    make_message,
)


def test_components_v2_payload_preserves_webhook_compatibility_fields() -> None:
    # Baseline characterization: invariant aspects that must survive the
    # richer-card renderer change. Passes before and after production edits.
    # Given
    message = make_message(embed_color=0)

    # When
    payload = build_payload(message)

    # Then - flags, mentions, username, avatar, accent color
    assert payload["flags"] == 32768
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["username"] == "RSS Bot"
    assert payload["avatar_url"] == "https://example.test/avatar.png"
    children = get_container_children(message)
    assert children[0]["type"] == 10
    assert children[0]["content"] == "## [Entry](https://example.test/entry)"
    assert children[1]["type"] == 10
    assert children[1]["content"] == "Description"
    assert children[2]["type"] == 14
    assert children[3]["type"] == 10
    # Then - title-link fallback for unsafe link
    unsafe_message = make_message()
    unsafe_message = replace(
        unsafe_message,
        entry=replace(unsafe_message.entry, link="javascript:alert(1)"),
    )
    unsafe_children = get_container_children(unsafe_message)
    assert unsafe_children[0]["content"] == "## Entry"
    # Then - no javascript: anywhere in serialized payload
    unsafe_payload = build_payload(unsafe_message)
    assert "javascript:" not in json.dumps(unsafe_payload)


def test_components_v2_payload_preserves_entry_and_webhook_fields() -> None:
    # Given
    message = make_message(embed_color=0)

    # When
    payload = build_payload(message)

    # Then
    assert payload == {
        "allowed_mentions": {"parse": []},
        "avatar_url": "https://example.test/avatar.png",
        "components": [
            {
                "accent_color": 0,
                "components": [
                    {
                        "content": "## [Entry](https://example.test/entry)",
                        "type": 10,
                    },
                    {"content": "Description", "type": 10},
                    {"divider": True, "spacing": 1, "type": 14},
                    {
                        "content": "-# News • By Author • <t:1784548800:R>",
                        "type": 10,
                    },
                ],
                "type": 17,
            },
        ],
        "flags": 32768,
        "username": "RSS Bot",
    }


def test_components_v2_payload_omits_empty_optional_text() -> None:
    # Given
    message = make_message()
    message = replace(
        message,
        entry=replace(message.entry, description="", author="", timestamp=None),
    )

    # When
    payload = build_payload(message)

    # Then
    components = payload["components"]
    assert isinstance(components, list)
    container = components[0]
    assert isinstance(container, dict)
    assert container["accent_color"] == 5814783
    assert container["components"] == [
        {"content": "## [Entry](https://example.test/entry)", "type": 10},
        {"divider": True, "spacing": 1, "type": 14},
        {"content": "-# News", "type": 10},
    ]


@pytest.mark.parametrize(
    ("link", "expected_heading"),
    [
        (
            "https://example.test/wiki/Foo_(bar)",
            "## [Entry](https://example.test/wiki/Foo_%28bar%29)",
        ),
        (
            "https://legit.example/) [Urgent](https://evil.example/phish",
            "## [Entry](https://legit.example/%29%20[Urgent]%28https://evil.example/phish)",
        ),
        ("javascript:alert(1)", "## Entry"),
        ("https://user:secret@example.test/article", "## Entry"),
        ("https://example.test:bad/article", "## Entry"),
        ("https://example.test:99999/article", "## Entry"),
    ],
)
def test_components_v2_payload_safely_renders_entry_links(
    link: str,
    expected_heading: str,
) -> None:
    # Given
    message = make_message()
    message = replace(message, entry=replace(message.entry, link=link))

    # When
    children = get_container_children(message)

    # Then
    heading = children[0]
    assert heading["content"] == expected_heading


@pytest.mark.parametrize(
    "title",
    [
        "[high] Swell freezes swETH and rswETH after suspected admin key compromise inflates their NAV",
    ],
)
def test_components_v2_payload_preserves_literal_title_characters(title: str) -> None:
    message = make_message()
    message = replace(message, entry=replace(message.entry, title=title))

    heading = get_container_children(message)[0]

    assert heading["content"] == f"## [{title}](https://example.test/entry)"


@pytest.mark.parametrize(
    "title",
    [
        "unbalanced ] close",
        "unbalanced [ open",
        "x](https://evil.test) [forged",
        r"title\](https://evil.test)",
        r"C:\News\Update [1]",
        "title\\",
        "nested [masked](https://evil.test) link",
        "autolink <https://evil.test>",
    ],
)
def test_components_v2_payload_escapes_unsafe_link_labels(title: str) -> None:
    message = make_message()
    message = replace(message, entry=replace(message.entry, title=title))

    heading = get_container_children(message)[0]

    escaped = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    assert heading["content"] == (f"## [{escaped}](https://example.test/entry)")


@pytest.mark.parametrize(
    "image_url",
    [
        "http://example.test/image.jpg",
        "https://preview.localhost/image.jpg",
        "https://localhost./image.jpg",
        "https://preview.localhost./image.jpg",
        "https://%6cocalhost/image.jpg",
        "https://localhost。/image.jpg",
        "https://ｌｏｃａｌｈｏｓｔ/image.jpg",
        "https://１２７。０。０。１/image.jpg",
        "https://127.0.0.1/image.jpg",
        "https://2130706433/image.jpg",
        "https://0177.0.0.1/image.jpg",
        "https://127.1/image.jpg",
        "https://0x7f000001/image.jpg",
        "https://example.com /image.jpg",
        "https://.example/image.jpg",
        "https://example..com/image.jpg",
        "https://-example.com/image.jpg",
        "https://example-.com/image.jpg",
        "https://exam_ple.com/image.jpg",
        f"https://{'a' * 64}.example/image.jpg",
        f"https://{'a.' * 126}aa/image.jpg",
        "https://169.254.169.254/latest/meta-data/",
        "https://224.0.0.1/image.jpg",
        "https://[::1]/image.jpg",
        "https://[ff02::1]/image.jpg",
        f"https://example.test/{'a' * 2_048}",
        f"https://example.test/{'é' * 400}",
    ],
)
def test_components_v2_payload_omits_unsafe_remote_thumbnails(
    image_url: str,
) -> None:
    # Given
    message = make_message()
    message = replace(message, entry=replace(message.entry, image_url=image_url))

    # When
    children = get_container_children(message)

    # Then
    assert children[0]["type"] == 10


@pytest.mark.parametrize(
    ("author", "source_title", "expected_metadata"),
    [
        (
            "[Official](https://evil.example)",
            "News",
            (
                "-# News • By \\[Official\\]\\(h\u200bttps://evil.example\\) • "
                "<t:1784548800:R>"
            ),
        ),
        (
            "Author",
            "[Trusted Source](https://evil.example)",
            (
                "-# \\[Trusted Source\\]\\(h\u200bttps://evil.example\\) • By Author • "
                "<t:1784548800:R>"
            ),
        ),
        (
            "https://evil.example/author",
            "www.evil.example/source",
            (
                "-# w\u200bww.evil.example/source • By h\u200bttps://evil.example/author • "
                "<t:1784548800:R>"
            ),
        ),
    ],
)
def test_components_v2_payload_escapes_metadata_links(
    author: str,
    source_title: str,
    expected_metadata: str,
) -> None:
    # Given
    message = make_message()
    message = replace(
        message,
        entry=replace(message.entry, author=author),
        source_title=source_title,
    )

    # When
    metadata = get_metadata_content(message)

    # Then
    assert metadata == expected_metadata


def test_components_v2_payload_preserves_description_markdown() -> None:
    # Given
    description = "Read the [documentation](https://example.test/docs) for **details**."
    message = make_message()
    message = replace(message, entry=replace(message.entry, description=description))

    # When
    contents = get_text_display_contents(message)

    # Then
    assert contents[1] == description


GALLERY_URL_ONE = "https://pbs.twimg.com/media/one.jpg"
GALLERY_URL_TWO = "https://pbs.twimg.com/media/two.jpg"
GALLERY_URL_THREE = "https://pbs.twimg.com/media/three.jpg"
THUMBNAIL_URL = "https://example.test/image.png"


def test_components_v2_payload_renders_single_image_gallery_full_width() -> None:
    # Given
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            image_url=THUMBNAIL_URL,
            image_urls=(GALLERY_URL_ONE,),
        ),
    )

    # When
    payload = build_payload(message)
    children = get_container_children(message)
    items = get_media_gallery_items(message)

    # Then - gallery replaces the thumbnail and sits before separator/footer
    assert [child["type"] for child in children] == [10, 10, 12, 14, 10]
    assert len(items) == 1
    assert items[0] == {
        "description": "Entry",
        "media": {"url": GALLERY_URL_ONE},
    }
    assert all(component.get("type") != 11 for component in get_all_components(message))
    # Then - the first gallery image is not duplicated as a section thumbnail
    assert THUMBNAIL_URL not in json.dumps(payload)


def test_components_v2_payload_renders_multi_image_gallery_grid() -> None:
    # Given
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            image_urls=(GALLERY_URL_ONE, GALLERY_URL_TWO, GALLERY_URL_THREE),
        ),
    )

    # When
    children = get_container_children(message)
    items = get_media_gallery_items(message)

    # Then
    assert [child["type"] for child in children] == [10, 10, 12, 14, 10]
    assert len(items) == 3
    assert items[0] == {
        "description": "Entry",
        "media": {"url": GALLERY_URL_ONE},
    }
    assert items[1] == {
        "description": "Entry",
        "media": {"url": GALLERY_URL_TWO},
    }
    assert items[2] == {
        "description": "Entry",
        "media": {"url": GALLERY_URL_THREE},
    }


def test_components_v2_payload_deduplicates_and_caps_gallery_items() -> None:
    # Given
    message = make_message()
    urls = (
        GALLERY_URL_ONE,
        *tuple(GALLERY_URL_TWO for _ in range(3)),
        *tuple(f"https://pbs.twimg.com/media/{index}.jpg" for index in range(15)),
    )
    message = replace(
        message,
        entry=replace(message.entry, image_urls=urls),
    )

    # When
    items = get_media_gallery_items(message)

    # Then
    assert len(items) == 10
    unique_urls = {
        item["media"]["url"] for item in items if isinstance(item["media"], dict)
    }
    assert len(unique_urls) == len(items)
    assert GALLERY_URL_ONE in unique_urls
    assert GALLERY_URL_TWO in unique_urls


def test_components_v2_payload_falls_back_from_empty_gallery_to_thumbnail() -> None:
    # Given - gallery URLs are unsafe, but image_url is safe
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            image_url=THUMBNAIL_URL,
            image_urls=("http://insecure.test/one.jpg",),
        ),
    )

    # When
    children = get_container_children(message)

    # Then - no gallery, section thumbnail is rendered instead
    assert not get_media_galleries(message)
    assert children[0]["type"] == 9
    assert get_thumbnail_url(message) == THUMBNAIL_URL


def test_components_v2_payload_falls_back_from_empty_gallery_to_no_image() -> None:
    # Given - gallery URLs are unsafe and there is no image_url
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            image_url=None,
            image_urls=("http://insecure.test/one.jpg",),
        ),
    )

    # When
    children = get_container_children(message)

    # Then - no image of any kind
    assert not get_media_galleries(message)
    assert all(child.get("type") != 11 for child in children)
    assert all(child.get("type") != 9 for child in children)
    assert [child["type"] for child in children] == [10, 10, 14, 10]


def test_components_v2_payload_keeps_single_thumbnail_when_image_urls_empty() -> None:
    # Given - existing providers leave image_urls empty and use image_url
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            image_url=THUMBNAIL_URL,
            image_urls=(),
        ),
    )

    # When
    payload = build_payload(message)
    children = get_container_children(message)

    # Then - payload is identical to the pre-gallery behaviour
    assert not get_media_galleries(message)
    assert children[0]["type"] == 9
    assert get_thumbnail_url(message) == THUMBNAIL_URL
    assert payload == {
        "allowed_mentions": {"parse": []},
        "avatar_url": "https://example.test/avatar.png",
        "components": [
            {
                "accent_color": 5814783,
                "components": [
                    {
                        "accessory": {
                            "description": "Entry",
                            "media": {"url": THUMBNAIL_URL},
                            "type": 11,
                        },
                        "components": [
                            {
                                "content": "## [Entry](https://example.test/entry)",
                                "type": 10,
                            },
                            {"content": "Description", "type": 10},
                        ],
                        "type": 9,
                    },
                    {"divider": True, "spacing": 1, "type": 14},
                    {
                        "content": "-# News • By Author • <t:1784548800:R>",
                        "type": 10,
                    },
                ],
                "type": 17,
            },
        ],
        "flags": 32768,
        "username": "RSS Bot",
    }


def test_components_v2_payload_does_not_render_gallery_for_attachments() -> None:
    # Given
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            image_urls=(GALLERY_URL_ONE,),
        ),
    )

    # When
    payload = build_components_v2_payload(
        message.feed,
        message.entry,
        message.source_title,
        attachment_filename="product-image.jpg",
    )
    components = payload["components"]
    assert isinstance(components, list)
    container = components[0]
    assert isinstance(container, dict)
    inner = container["components"]
    assert isinstance(inner, list)
    children: list[dict[str, JSONValue]] = []
    for child in inner:
        assert isinstance(child, dict)
        children.append(child)

    # Then - attachment thumbnail is kept, gallery is dropped
    assert not any(child.get("type") == 12 for child in children)
    assert children[0]["type"] == 9
    assert get_thumbnail_url_from_children(children) == "attachment://product-image.jpg"


def test_components_v2_payload_gallery_text_budget_still_respected() -> None:
    # Given - a huge description alongside a gallery
    message = make_message()
    message = replace(
        message,
        entry=replace(
            message.entry,
            image_urls=(GALLERY_URL_ONE,),
            description="D" * 5000,
            title="T" * 5000,
            author="A" * 5000,
        ),
        source_title="S" * 5000,
    )

    # When
    contents = get_text_display_contents(message)
    galleries = get_media_galleries(message)

    # Then - text stays within budget and gallery still renders
    assert sum(map(len, contents)) <= 4000
    assert len(galleries) == 1
    item = get_media_gallery_items(message)[0]
    assert item["media"] == {"url": GALLERY_URL_ONE}
    description = item["description"]
    assert isinstance(description, str)
    assert len(description) <= MAX_THUMBNAIL_DESCRIPTION_CHARACTERS
