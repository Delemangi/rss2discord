from rss2discord.configuration import FeedAdapterName, FeedConfig, FeedStrategyName
from rss2discord.discord.client import WebhookMessage
from rss2discord.discord.components import JSONValue, build_components_v2_payload
from rss2discord.models import EntryData


def make_message(
    *,
    embed_color: int | None = None,
    strategy: FeedStrategyName = "rss",
    adapter: FeedAdapterName | None = None,
    url: str = "https://example.test/feed.xml",
    webhook_name: str | None = "RSS Bot",
    webhook_avatar: str | None = "https://example.test/avatar.png",
    entry: EntryData | None = None,
    source_title: str = "News",
) -> WebhookMessage:
    return WebhookMessage(
        feed=FeedConfig(
            id="news",
            name="News",
            url=url,
            webhook="https://discord.test/api/webhooks/id/token?thread_id=123",
            webhook_name=webhook_name,
            webhook_avatar=webhook_avatar,
            embed_color=embed_color,
            strategy=strategy,
            adapter=adapter,
        ),
        entry=entry
        or EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="Description",
            author="Author",
            timestamp="2026-07-20T12:00:00+00:00",
        ),
        source_title=source_title,
    )


def build_payload(message: WebhookMessage) -> dict[str, JSONValue]:
    return build_components_v2_payload(
        message.feed,
        message.entry,
        message.source_title,
    )


def get_text_display_contents(message: WebhookMessage) -> list[str]:
    contents: list[str] = []
    for component in get_all_components(message):
        if component.get("type") != 10:
            continue
        content = component.get("content")
        if isinstance(content, str):
            contents.append(content)
    return contents


def get_container(message: WebhookMessage) -> dict[str, JSONValue]:
    payload = build_payload(message)
    components = payload["components"]
    assert isinstance(components, list)
    container = components[0]
    assert isinstance(container, dict)
    return container


def get_container_children(message: WebhookMessage) -> list[dict[str, JSONValue]]:
    container = get_container(message)
    children = container["components"]
    assert isinstance(children, list)
    result: list[dict[str, JSONValue]] = []
    for child in children:
        assert isinstance(child, dict)
        result.append(child)
    return result


def get_all_components(message: WebhookMessage) -> list[dict[str, JSONValue]]:
    payload = build_payload(message)
    components = payload["components"]
    assert isinstance(components, list)

    result: list[dict[str, JSONValue]] = []
    for component in components:
        assert isinstance(component, dict)
        result.extend(_component_and_descendants(component))
    return result


def _component_and_descendants(
    component: dict[str, JSONValue],
) -> list[dict[str, JSONValue]]:
    result = [component]
    children = component.get("components")
    if isinstance(children, list):
        for child in children:
            assert isinstance(child, dict)
            result.extend(_component_and_descendants(child))

    accessory = component.get("accessory")
    if isinstance(accessory, dict):
        result.extend(_component_and_descendants(accessory))
    return result


def get_metadata_content(message: WebhookMessage) -> str:
    children = get_container_children(message)
    metadata = children[-1]
    content = metadata["content"]
    assert isinstance(content, str)
    return content


def get_metrics_content(message: WebhookMessage) -> str:
    """Return the metrics Text Display, which always follows the heading."""
    return get_text_display_contents(message)[1]


def get_child_contents(component: dict[str, JSONValue]) -> list[str]:
    """Return the Text Display contents nested directly inside a component."""
    children = component["components"]
    assert isinstance(children, list)
    contents: list[str] = []
    for child in children:
        assert isinstance(child, dict)
        content = child["content"]
        assert isinstance(content, str)
        contents.append(content)
    return contents


def get_media_galleries(message: WebhookMessage) -> list[dict[str, JSONValue]]:
    """Return every Media Gallery component in the payload."""
    return [
        component
        for component in get_all_components(message)
        if component.get("type") == 12
    ]


def get_media_gallery_items(
    message: WebhookMessage,
) -> list[dict[str, JSONValue]]:
    """Return the media items from the first Media Gallery, if any."""
    galleries = get_media_galleries(message)
    if not galleries:
        return []
    items = galleries[0].get("items")
    assert isinstance(items, list)
    result: list[dict[str, JSONValue]] = []
    for item in items:
        assert isinstance(item, dict)
        result.append(item)
    return result


def get_thumbnail_url(message: WebhookMessage) -> str:
    """Return the Section thumbnail URL from the first container child."""
    return get_thumbnail_url_from_children(get_container_children(message))


def get_thumbnail_url_from_children(children: list[dict[str, JSONValue]]) -> str:
    """Return the Section thumbnail URL from already-built container children."""
    accessory = children[0]["accessory"]
    assert isinstance(accessory, dict)
    media = accessory.get("media")
    assert isinstance(media, dict)
    url = media.get("url")
    assert isinstance(url, str)
    return url
