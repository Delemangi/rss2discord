from dataclasses import replace

import pytest
import requests

from configuration import FeedConfig
from discord_client import DiscordWebhookClient, JSONValue, WebhookMessage
from models import EntryData

type PostArgument = int | dict[str, str] | dict[str, JSONValue]


def make_message(*, embed_color: int | None = None) -> WebhookMessage:
    return WebhookMessage(
        feed=FeedConfig(
            id="news",
            name="News",
            url="https://example.test/feed.xml",
            webhook="https://discord.test/api/webhooks/id/token?thread_id=123",
            webhook_name="RSS Bot",
            webhook_avatar="https://example.test/avatar.png",
            embed_color=embed_color,
        ),
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="Description",
            author="Author",
            timestamp="2026-07-20T12:00:00+00:00",
        ),
        source_title="News",
    )


def make_response(status_code: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    return response


def test_components_v2_payload_preserves_entry_and_webhook_fields() -> None:
    # Given
    message = make_message(embed_color=0)

    # When
    payload = DiscordWebhookClient._build_payload(message)

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
                    {"type": 14},
                    {
                        "content": "-# By Author • News • <t:1784548800:R>",
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
    payload = DiscordWebhookClient._build_payload(message)

    # Then
    components = payload["components"]
    assert isinstance(components, list)
    container = components[0]
    assert isinstance(container, dict)
    assert container["accent_color"] == 5814783
    assert container["components"] == [
        {"content": "## [Entry](https://example.test/entry)", "type": 10},
        {"type": 14},
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
    payload = DiscordWebhookClient._build_payload(message)

    # Then
    components = payload["components"]
    assert isinstance(components, list)
    container = components[0]
    assert isinstance(container, dict)
    children = container["components"]
    assert isinstance(children, list)
    heading = children[0]
    assert isinstance(heading, dict)
    assert heading["content"] == expected_heading


@pytest.mark.parametrize(
    ("author", "source_title", "expected_metadata"),
    [
        (
            "[Official](https://evil.example)",
            "News",
            r"-# By \[Official\]\(https://evil.example\) • News • <t:1784548800:R>",
        ),
        (
            "Author",
            "[Trusted Source](https://evil.example)",
            r"-# By Author • \[Trusted Source\]\(https://evil.example\) • <t:1784548800:R>",
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
    payload = DiscordWebhookClient._build_payload(message)

    # Then
    components = payload["components"]
    assert isinstance(components, list)
    container = components[0]
    assert isinstance(container, dict)
    children = container["components"]
    assert isinstance(children, list)
    metadata = children[-1]
    assert isinstance(metadata, dict)
    assert metadata["content"] == expected_metadata


def test_delivery_enables_components_v2_for_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    arguments: dict[str, PostArgument] = {}

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        del url
        arguments.update(kwargs)
        return make_response(204)

    monkeypatch.setattr(session, "post", post)

    # When
    delivered = DiscordWebhookClient(session).send(make_message(), lambda _: True)

    # Then
    assert delivered
    assert arguments["params"] == {"with_components": "true"}
