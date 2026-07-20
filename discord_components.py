import re
from collections.abc import Callable
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
MAX_TEXT_DISPLAY_CHARACTERS: Final = 4000
ELLIPSIS: Final = "…"
MIN_HEADING_CHARACTERS: Final = len(f"## {ELLIPSIS}")
MIN_METADATA_CHARACTERS: Final = len(f"-# {ELLIPSIS}")
MAX_DESCRIPTION_CHARACTERS: Final = (
    MAX_TEXT_DISPLAY_CHARACTERS - MIN_HEADING_CHARACTERS - MIN_METADATA_CHARACTERS
)
BARE_LINK_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"\b(?:https?://|www\.)",
    re.IGNORECASE,
)


def build_components_v2_payload(
    feed: FeedConfig,
    entry: EntryData,
    source_title: str,
) -> dict[str, JSONValue]:
    title = _escape_markdown_link_text(entry.title)
    link = _safe_markdown_url(entry.link)
    plain_heading = f"## {title}"
    heading = f"## [{title}]({link})" if link is not None else plain_heading
    description = _bounded_description(entry.description)
    metadata = _build_metadata(entry, source_title)

    if (
        link is not None
        and len(heading) + len(description) + len(metadata)
        > MAX_TEXT_DISPLAY_CHARACTERS
    ):
        heading = plain_heading

    if len(heading) + len(description) + len(metadata) > MAX_TEXT_DISPLAY_CHARACTERS:
        text_budget = MAX_TEXT_DISPLAY_CHARACTERS - len(description)
        if len(heading) <= text_budget - MIN_METADATA_CHARACTERS:
            metadata = _truncate_rendered_text(metadata, text_budget - len(heading))
        elif len(metadata) <= text_budget - MIN_HEADING_CHARACTERS:
            heading = _truncate_heading(entry.title, text_budget - len(metadata))
        else:
            heading_limit = max(MIN_HEADING_CHARACTERS, text_budget // 2)
            heading = _truncate_heading(entry.title, heading_limit)
            metadata = _truncate_rendered_text(metadata, text_budget - len(heading))

    container_components: list[JSONValue] = [
        {"content": heading, "type": TEXT_DISPLAY_COMPONENT},
    ]
    if description:
        container_components.append(
            {"content": description, "type": TEXT_DISPLAY_COMPONENT},
        )

    container_components.extend(
        [
            {"type": SEPARATOR_COMPONENT},
            {
                "content": metadata,
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


def _escape_metadata_text(text: str) -> str:
    return _escape_markdown_text(
        BARE_LINK_PREFIX.sub(
            lambda match: f"{match.group(0)[0]}\u200b{match.group(0)[1:]}",
            text,
        ),
    )


def _bounded_description(description: str) -> str:
    if len(description) <= MAX_DESCRIPTION_CHARACTERS:
        return description
    return _truncate_rendered_text(description, MAX_DESCRIPTION_CHARACTERS)


def _build_metadata(entry: EntryData, source_title: str) -> str:
    parts = []
    if entry.author:
        parts.append(f"By {_escape_metadata_text(entry.author)}")
    parts.append(_escape_metadata_text(source_title))
    if entry.timestamp is not None:
        parts.append(_format_timestamp(entry.timestamp))
    return f"-# {' • '.join(parts)}"


def _truncate_heading(title: str, max_length: int) -> str:
    prefix = "## "
    return prefix + _truncate_escaped_text(
        title,
        max_length - len(prefix),
        _escape_markdown_link_text,
    )


def _truncate_escaped_text(
    text: str,
    max_length: int,
    escape: Callable[[str], str],
) -> str:
    escaped = escape(text)
    if len(escaped) <= max_length:
        return escaped
    if max_length <= len(ELLIPSIS):
        return ELLIPSIS[:max_length]

    low = 0
    high = len(text)
    best = ELLIPSIS
    while low <= high:
        midpoint = (low + high) // 2
        candidate = escape(text[:midpoint]) + ELLIPSIS
        if len(candidate) <= max_length:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _truncate_rendered_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    if max_length <= len(ELLIPSIS):
        return ELLIPSIS[:max_length]
    return text[: max_length - len(ELLIPSIS)].rstrip("\\") + ELLIPSIS


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
        return _escape_metadata_text(timestamp)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    return f"<t:{int(published_at.timestamp())}:R>"
