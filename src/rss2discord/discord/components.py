import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote, urlsplit

from rss2discord.configuration import FeedConfig
from rss2discord.discord.source_labels import source_label
from rss2discord.models import EntryData, PriceDirection, SourceMetric

type JSONValue = (
    bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None
)

DEFAULT_ACCENT_COLOR: Final = 5814783
# A price move states its own direction through the accent bar, so the reader
# can tell a drop from a rise before parsing any numbers. Read from the buyer's
# side: cheaper is good news.
PRICE_DECREASE_ACCENT_COLOR: Final = 0x3BA55D
PRICE_INCREASE_ACCENT_COLOR: Final = 0xED4245
IS_COMPONENTS_V2: Final = 1 << 15
TEXT_DISPLAY_COMPONENT: Final = 10
SECTION_COMPONENT: Final = 9
THUMBNAIL_COMPONENT: Final = 11
SEPARATOR_COMPONENT: Final = 14
CONTAINER_COMPONENT: Final = 17

# Discord counts every Text Display in the message against one shared budget.
MAX_TEXT_DISPLAY_CHARACTERS: Final = 4000
MAX_THUMBNAIL_DESCRIPTION_CHARACTERS: Final = 1024

# Per-block ceilings. They sum to well under the shared budget so the
# description always keeps a usable share no matter how hostile an entry is.
MAX_HEADING_CHARACTERS: Final = 1024
MAX_HEADING_TITLE_CHARACTERS: Final = 300
MAX_METRICS_CHARACTERS: Final = 512
MAX_FOOTER_LINE_CHARACTERS: Final = 512
MAX_METRIC_LABEL_CHARACTERS: Final = 48
MAX_METRIC_VALUE_CHARACTERS: Final = 64
# The densest producers (Neptun and Neksio price alerts) emit six metrics, so
# this keeps headroom above them: a card overflowing the cap loses its tail
# silently, and that is a worse failure than a slightly longer subtext line.
MAX_RENDERED_METRICS: Final = 8
# A well-formed <t:...:R> is tiny; this only bounds unparseable timestamps,
# which fall back to being echoed verbatim.
MAX_FOOTER_TIMESTAMP_CHARACTERS: Final = 128

SEPARATOR_SPACING_COMPACT: Final = 1
METADATA_SEPARATOR: Final = " • "
SUBTEXT_PREFIX: Final = "-# "
ELLIPSIS: Final = "…"
ATTACHMENT_FILENAMES: Final = frozenset(
    {
        "product-image.gif",
        "product-image.jpg",
        "product-image.png",
        "product-image.webp",
    },
)
BARE_LINK_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"\b(?:https?://|www\.)",
    re.IGNORECASE,
)
LINE_BREAK: Final[re.Pattern[str]] = re.compile("[\r\n\v\f\u2028\u2029]+")


def build_components_v2_payload(
    feed: FeedConfig,
    entry: EntryData,
    source_title: str,
    *,
    attachment_filename: str | None = None,
) -> dict[str, JSONValue]:
    """Render an entry as a Components V2 container.

    The card is ordered by what a reader needs first: title, then the entry's
    metrics, then its description, and only then the provenance footer. The
    first entry in ``source_metrics`` is treated as the headline metric and is
    rendered at body size; every transport already orders its metrics that way.
    """
    link = _safe_markdown_url(entry.link)
    heading = _build_heading(entry.title, link)
    metrics = _build_metrics(entry.source_metrics)
    footer = _build_footer(entry, feed, source_title, link)
    description = _truncate_rendered_text(
        entry.description,
        _description_budget(heading, metrics, footer),
    )

    safe_image_url = _resolve_image_url(entry, attachment_filename)

    container_components: list[JSONValue] = []
    heading_component = _text_display(heading)
    metrics_component = _text_display(metrics) if metrics is not None else None
    description_component = _text_display(description) if description else None

    if safe_image_url is not None:
        # A Section pairs its children with the thumbnail. Keep it to the title
        # and the metrics so a long description gets the full container width.
        section_children: list[JSONValue] = [heading_component]
        trailing: list[JSONValue] = []
        if metrics_component is not None:
            section_children.append(metrics_component)
            if description_component is not None:
                trailing.append(description_component)
        elif description_component is not None:
            section_children.append(description_component)
        container_components.append(
            {
                "accessory": {
                    "description": _thumbnail_description(entry.title),
                    "media": {"url": safe_image_url},
                    "type": THUMBNAIL_COMPONENT,
                },
                "components": section_children,
                "type": SECTION_COMPONENT,
            },
        )
        container_components.extend(trailing)
    else:
        container_components.append(heading_component)
        if metrics_component is not None:
            container_components.append(metrics_component)
        if description_component is not None:
            container_components.append(description_component)

    container_components.append(
        {
            "divider": True,
            "spacing": SEPARATOR_SPACING_COMPACT,
            "type": SEPARATOR_COMPONENT,
        },
    )
    container_components.append(_text_display(footer))

    payload: dict[str, JSONValue] = {
        "allowed_mentions": {"parse": []},
        "components": [
            {
                "accent_color": _accent_color(feed, entry),
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


def _text_display(content: str) -> dict[str, JSONValue]:
    return {"content": content, "type": TEXT_DISPLAY_COMPONENT}


def _accent_color(feed: FeedConfig, entry: EntryData) -> int:
    """Direction wins over the feed's colour: which way the price moved is the
    whole point of the alert, and matters more than per-feed branding."""
    match entry.price_direction:
        case PriceDirection.DECREASE:
            return PRICE_DECREASE_ACCENT_COLOR
        case PriceDirection.INCREASE:
            return PRICE_INCREASE_ACCENT_COLOR
        case None:
            pass
    if feed.embed_color is not None:
        return feed.embed_color
    return DEFAULT_ACCENT_COLOR


def _resolve_image_url(
    entry: EntryData,
    attachment_filename: str | None,
) -> str | None:
    if attachment_filename is None:
        return _safe_markdown_url(entry.image_url) if entry.image_url else None
    if attachment_filename in ATTACHMENT_FILENAMES:
        return f"attachment://{attachment_filename}"
    return None


def _description_budget(heading: str, metrics: str | None, footer: str) -> int:
    used = len(heading) + len(footer)
    if metrics is not None:
        used += len(metrics)
    return max(0, MAX_TEXT_DISPLAY_CHARACTERS - used)


def _build_heading(title: str, safe_link: str | None) -> str:
    # A heading occupies a single line, so a break in the title would let feed
    # text close it and forge further lines - including a counterfeit of the
    # grey provenance footer - inside the container.
    title = LINE_BREAK.sub(" ", title)
    # Bound the visible title on its own. A URL costs budget but no width, so
    # measuring the whole linked construct would strip the link off entries
    # whose address merely carries a long query string.
    escaped = _escape_markdown_link_text(title)
    if len(escaped) > MAX_HEADING_TITLE_CHARACTERS:
        escaped = _truncate_escaped_text(
            title,
            MAX_HEADING_TITLE_CHARACTERS,
            _escape_markdown_link_text,
        )
    if safe_link is not None:
        # Link only when the whole construct fits: truncating a Markdown link
        # would leave a mangled URL or broken syntax behind.
        linked = f"## [{escaped}]({safe_link})"
        if len(linked) <= MAX_HEADING_CHARACTERS:
            return linked
    return f"## {escaped}"


def _build_metrics(metrics: tuple[SourceMetric, ...]) -> str | None:
    if not metrics:
        return None
    rendered = metrics[:MAX_RENDERED_METRICS]
    prior_index = next(
        (index for index, metric in enumerate(rendered) if index > 0 and metric.prior),
        None,
    )
    prior = rendered[prior_index] if prior_index is not None else None
    headline = _render_headline_metric(rendered[0], prior)
    supporting = [
        _render_metric(metric)
        for index, metric in enumerate(rendered)
        if index > 0 and index != prior_index
    ]

    # One supporting metric per line: a run of values joined by separators reads
    # as a sentence, where the reader has to parse it to find the one they want.
    # Discord rejects an empty Text Display, and a blank headline must not leave
    # the block opening on a bare newline, so only rendered lines are kept.
    lines = [headline] if headline else []
    remaining = MAX_METRICS_CHARACTERS - len(headline)
    for text in supporting:
        if not text:
            continue
        line = f"{SUBTEXT_PREFIX}{text}"
        cost = len(line) + (len("\n") if lines else 0)
        if cost > remaining:
            # Skip rather than stop: one overlong detail should not cost the
            # reader every shorter one behind it.
            continue
        remaining -= cost
        lines.append(line)
    if not lines:
        return None
    return "\n".join(lines)


def _render_headline_metric(metric: SourceMetric, prior: SourceMetric | None) -> str:
    label, value = _metric_parts(metric)
    if not value:
        rendered = label
    elif not label:
        rendered = f"**{value}**"
    else:
        rendered = f"{label}: **{value}**"
    if prior is None:
        return rendered
    # The strike-through says "was" on its own, so the prior metric's own label
    # would only repeat what the styling already communicates.
    _, prior_value = _metric_parts(prior)
    if not prior_value:
        return rendered
    return f"{rendered} ~~{prior_value}~~"


def _render_metric(metric: SourceMetric) -> str:
    label, value = _metric_parts(metric)
    if not label:
        return value
    if not value:
        return label
    return f"{label}: {value}"


def _metric_parts(metric: SourceMetric) -> tuple[str, str]:
    # Clip before escaping so a clipped value can never split an escape pair,
    # which would leak a stray backslash into the rendered card.
    label = _escape_metadata_text(
        _truncate_rendered_text(metric.label, MAX_METRIC_LABEL_CHARACTERS),
    )
    value = _escape_metadata_text(
        _truncate_rendered_text(metric.value, MAX_METRIC_VALUE_CHARACTERS),
    )
    return label, value


def _build_footer(
    entry: EntryData,
    feed: FeedConfig,
    source_title: str,
    safe_primary_link: str | None,
) -> str:
    """Build the single provenance line, led by the source the entry came from.

    The timestamp closes the line, after the categories.
    """
    label = source_label(feed)
    parts: list[str] = [label]
    if source_title and source_title.strip().casefold() != label.casefold():
        parts.append(_escape_metadata_text(source_title))
    if entry.author:
        parts.append(f"By {_escape_metadata_text(entry.author)}")
    safe_discussion_url = (
        _safe_markdown_url(entry.discussion_url) if entry.discussion_url else None
    )
    if safe_discussion_url is not None and safe_discussion_url != safe_primary_link:
        parts.append(f"[Discussion]({safe_discussion_url})")
    parts.extend(_escape_metadata_text(category) for category in entry.categories)

    timestamp = (
        _truncate_rendered_text(
            _format_timestamp(entry.timestamp),
            MAX_FOOTER_TIMESTAMP_CHARACTERS,
        )
        if entry.timestamp is not None
        else None
    )

    line_budget = MAX_FOOTER_LINE_CHARACTERS - len(SUBTEXT_PREFIX)
    if timestamp is not None:
        # Reserve the tail so a long category list cannot crowd the time out.
        line_budget -= len(METADATA_SEPARATOR) + len(timestamp)

    line = _bounded_join(parts, line_budget)
    if line is None:
        line = _truncate_rendered_text(label, max(0, line_budget))
    if timestamp is not None:
        line = f"{line}{METADATA_SEPARATOR}{timestamp}" if line else timestamp
    return f"{SUBTEXT_PREFIX}{line}"


def _bounded_join(parts: Sequence[str], budget: int) -> str | None:
    """Join what fits, dropping whole parts so Markdown is never cut mid-token."""
    if budget <= 0:
        return None
    kept: list[str] = []
    for part in parts:
        candidate = METADATA_SEPARATOR.join([*kept, part])
        if len(candidate) > budget:
            # Skip rather than stop: one overlong category should not cost the
            # reader every shorter one behind it.
            continue
        kept.append(part)
    if not kept:
        return None
    return METADATA_SEPARATOR.join(kept)


def _escape_markdown_link_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _escape_markdown_text(text: str) -> str:
    escaped = text.replace("\\", "\\\\")
    for character in ("`", "*", "_", "~", "|", ">", "[", "]", "(", ")"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _escape_metadata_text(text: str) -> str:
    # Metadata is single-line by contract. A line break would close the "-# "
    # subtext block, letting feed text open a heading of its own inside the
    # card, so every break becomes a space before anything else is escaped.
    single_line = LINE_BREAK.sub(" ", text)
    return _escape_markdown_text(
        BARE_LINK_PREFIX.sub(
            lambda match: f"{match.group(0)[0]}\u200b{match.group(0)[1:]}",
            single_line,
        ),
    )


def _thumbnail_description(title: str) -> str:
    return _truncate_rendered_text(
        title,
        MAX_THUMBNAIL_DESCRIPTION_CHARACTERS,
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
