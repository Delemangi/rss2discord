from configuration import FeedConfig
from discord_client import DiscordWebhookClient, WebhookMessage
from models import EntryData


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


def get_text_display_contents(message: WebhookMessage) -> list[str]:
    payload = DiscordWebhookClient._build_payload(message)
    components = payload["components"]
    assert isinstance(components, list)
    container = components[0]
    assert isinstance(container, dict)
    children = container["components"]
    assert isinstance(children, list)

    contents: list[str] = []
    for child in children:
        assert isinstance(child, dict)
        content = child.get("content")
        if isinstance(content, str):
            contents.append(content)
    return contents
