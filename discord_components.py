from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote, urlsplit

from configuration import FeedConfig
from models import EntryData

type JSONValue = (
    None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
)

DEFAULT_ACCENT_COLOR: Final = 5814783
IS_COMPONENTS_V2: Final = 1 << 15
TEXT_DISPLAY_COMPONENT: Final = 10
SEPARATOR_COMPONENT: Final = 14
CONTAINER_COMPONENT: Final = 17


def build_components_v2_payload(
    feed: FeedConfig,
    entry: EntryData,
    source_title: str,
) -> dict[str, JSONValue]:
    title = _escape_markdown_link_text(entry.title)
    link = _safe_markdown_url(entry.link)
    heading = f"## [{title}]({link})" if link is not None else f"## {title}"
    container_components: list[JSONValue] = [
        {"content": heading, "type": TEXT_DISPLAY_COMPONENT},
    ]
    if entry.description:
        container_components.append(
            {"content": entry.description, "type": TEXT_DISPLAY_COMPONENT},
        )

    metadata = []
    if entry.author:
        metadata.append(f"By {_escape_markdown_text(entry.author)}")
    metadata.append(_escape_markdown_text(source_title))
    if entry.timestamp is not None:
        metadata.append(_format_timestamp(entry.timestamp))
    container_components.extend(
        [
            {"type": SEPARATOR_COMPONENT},
            {
                "content": f"-# {' • '.join(metadata)}",
                "type": TEXT_DISPLAY_COMPONENT,
            },
        ],
    )

    payload: dict[str, JSONValue] = {
        "allowed_mentions": {"parse": []},
        "components": [
            {
                "accent_color": (
                    feed.embed_color
                    if feed.embed_color is not None
                    else DEFAULT_ACCENT_COLOR
                ),
                "components": container_components,
                "type": CONTAINER_COMPONENT,
            },
        ],
        "flags": IS_COMPONENTS_V2,
    }
    if feed.webhook_name:
        payload["username"] = feed.webhook_name
    if feed.webhook_avatar:
        payload["avatar_url"] = feed.webhook_avatar
    return payload


def _escape_markdown_link_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _escape_markdown_text(text: str) -> str:
    escaped = text.replace("\\", "\\\\")
    for character in ("`", "*", "_", "~", "|", ">", "[", "]", "(", ")"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _safe_markdown_url(url: str) -> str | None:
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        return None
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or hostname is None:
        return None
    return quote(url, safe=":/?#[]@!$&'*+,;=%-._~")


def _format_timestamp(timestamp: str) -> str:
    try:
        published_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return _escape_markdown_text(timestamp)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    return f"<t:{int(published_at.timestamp())}:R>"
