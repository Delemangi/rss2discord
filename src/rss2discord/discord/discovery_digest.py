"""Discovery digest: compact cards plus searchable HTML/CSV attachments."""

from __future__ import annotations

import csv
import io
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import replace
from html import escape
from typing import Final

from rss2discord.configuration import FeedConfig
from rss2discord.discord.components import (
    MAX_TEXT_DISPLAY_CHARACTERS,
    TEXT_DISPLAY_COMPONENT,
    JSONValue,
    build_components_v2_payload,
)
from rss2discord.discord.message import ReportFile, WebhookMessage
from rss2discord.discord.report import (
    csv_safe,
    html_shell,
    write_bounded_rows,
)
from rss2discord.models import EntryData

INLINE_ENTRY_LIMIT: Final = 25
MAX_DIGEST_CARDS: Final = 3
MAX_DIGEST_CATEGORIES: Final = 5
CSV_HEADER: Final = (
    "feed",
    "entry_id",
    "title",
    "link",
    "author",
    "published",
    "categories",
    "description",
)
DISCOVERY_HEADERS: Final = (
    ("Title", "text"),
    ("Author", "text"),
    ("Published", "text"),
    ("Categories", "text"),
    ("Link", "text"),
)


def build_discovery_digest(
    feed: FeedConfig,
    entries: Sequence[tuple[str, EntryData]],
    source_title: str,
) -> WebhookMessage:
    """Build one bounded discovery message with cards and complete reports."""
    if not entries:
        raise ValueError("discovery digest requires at least one entry")
    observed_at = int(time.time())
    bounded_source = _single_line(source_title, 160)
    header = _digest_header(entries, bounded_source, observed_at)
    html_report, csv_report = build_discovery_reports(
        feed.id,
        entries,
        observed_at,
    )
    return WebhookMessage(
        feed=feed,
        entry=EntryData(
            title="New items",
            link=feed.url,
            description=header,
            author="",
            timestamp=None,
        ),
        source_title=bounded_source,
        files=(html_report, csv_report),
        components=(
            {"content": header, "type": TEXT_DISPLAY_COMPONENT},
            *_digest_cards(feed, entries, bounded_source, len(header)),
        ),
    )


def build_discovery_reports(
    feed_id: str,
    entries: Sequence[tuple[str, EntryData]],
    observed_at: int,
) -> tuple[ReportFile, ReportFile]:
    """Render every represented entry as searchable HTML and CSV."""
    suffix = f"{_safe_filename_part(feed_id)}-{observed_at}"
    return (
        ReportFile(
            filename=f"new-items-{suffix}.html",
            content=_html_report(feed_id, entries).encode("utf-8"),
            content_type="text/html; charset=utf-8",
        ),
        ReportFile(
            filename=f"new-items-{suffix}.csv",
            content=_csv_report(feed_id, entries).encode("utf-8"),
            content_type="text/csv; charset=utf-8",
        ),
    )


def _digest_header(
    entries: Sequence[tuple[str, EntryData]],
    source_title: str,
    observed_at: int,
) -> str:
    lines = [
        "## New items",
        f"**{len(entries):,}** new items in {source_title}",
        f"Observed <t:{observed_at}:f>",
    ]
    categories = _category_counts(entries)
    if categories:
        lines.append(
            "Top categories: "
            + " · ".join(f"{name} ({count:,})" for name, count in categories),
        )
    lines.append("The complete searchable list is attached (HTML and CSV).")
    return _bounded("\n".join(lines), MAX_TEXT_DISPLAY_CHARACTERS)


def _category_counts(
    entries: Sequence[tuple[str, EntryData]],
) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for _entry_id, entry in entries:
        category = entry.categories[0] if entry.categories else "Uncategorized"
        counter[category] += 1
    return counter.most_common(MAX_DIGEST_CATEGORIES)


def _digest_cards(
    feed: FeedConfig,
    entries: Sequence[tuple[str, EntryData]],
    source_title: str,
    header_characters: int,
) -> tuple[JSONValue, ...]:
    selected = entries[:MAX_DIGEST_CARDS]
    fixed = tuple(
        _card_components(feed, entry, source_title, "") for _entry_id, entry in selected
    )
    fixed_characters = sum(_text_characters(card) for card in fixed)
    budget = MAX_TEXT_DISPLAY_CHARACTERS - header_characters - fixed_characters
    while fixed and budget < 0:
        fixed = fixed[:-1]
        fixed_characters = sum(_text_characters(card) for card in fixed)
        budget = MAX_TEXT_DISPLAY_CHARACTERS - header_characters - fixed_characters
    if not fixed:
        return ()
    description_limit = max(0, budget // len(fixed))
    return tuple(
        component
        for _entry_id, entry in selected[: len(fixed)]
        for component in _card_components(
            feed,
            entry,
            source_title,
            entry.description[:description_limit],
        )
    )


def _card_components(
    feed: FeedConfig,
    entry: EntryData,
    source_title: str,
    description: str,
) -> tuple[JSONValue, ...]:
    payload = build_components_v2_payload(
        feed,
        replace(entry, description=description, image_url=None),
        source_title,
    )
    components = payload.get("components")
    return tuple(components) if isinstance(components, list) else ()


def _text_characters(components: Iterable[JSONValue]) -> int:
    total = 0
    for component in components:
        if not isinstance(component, dict):
            continue
        content = component.get("content")
        if component.get("type") == TEXT_DISPLAY_COMPONENT and isinstance(
            content,
            str,
        ):
            total += len(content)
        children = component.get("components")
        if isinstance(children, list):
            total += _text_characters(children)
    return total


def _html_report(
    feed_id: str,
    entries: Sequence[tuple[str, EntryData]],
) -> str:
    body = io.StringIO()
    written = write_bounded_rows(
        body,
        entries,
        write_row=lambda item: body.write(_html_row(item)),
        write_truncation=lambda omitted: body.write(
            f'<tr class="truncated"><td colspan="5">TRUNCATED: '
            f"{omitted:,} rows omitted</td></tr>",
        ),
    )
    return html_shell(
        feed_id,
        "New items",
        DISCOVERY_HEADERS,
        body.getvalue(),
        written,
    )


def _html_row(item: tuple[str, EntryData]) -> str:
    _entry_id, entry = item
    title = escape(_single_line(entry.title, 300))
    author = escape(_single_line(entry.author, 160))
    published = escape(entry.timestamp or "")
    categories = escape(_single_line(" | ".join(entry.categories), 300))
    link = escape(entry.link, quote=True)
    search = escape(
        _single_line(
            f"{entry.title} {entry.author} {' '.join(entry.categories)} "
            f"{entry.description}",
            1200,
        ).lower(),
        quote=True,
    )
    return (
        f'<tr data-search="{search}">'
        f'<td><a href="{link}" target="_blank" rel="noreferrer">{title}</a></td>'
        f"<td>{author}</td><td>{published}</td><td>{categories}</td>"
        f'<td><a href="{link}" target="_blank" rel="noreferrer">Open</a></td>'
        f"</tr>\n"
    )


def _csv_report(
    feed_id: str,
    entries: Sequence[tuple[str, EntryData]],
) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    write_bounded_rows(
        buffer,
        entries,
        write_row=lambda item: writer.writerow(_csv_row(feed_id, item)),
        write_truncation=lambda omitted: writer.writerow(
            (
                "",
                "",
                f"TRUNCATED: {omitted:,} rows omitted",
                "",
                "",
                "",
                "",
                "",
            ),
        ),
    )
    return buffer.getvalue()


def _csv_row(feed_id: str, item: tuple[str, EntryData]) -> tuple[str, ...]:
    entry_id, entry = item
    return tuple(
        csv_safe(value)
        for value in (
            feed_id,
            entry_id,
            entry.title,
            entry.link,
            entry.author,
            entry.timestamp or "",
            " | ".join(entry.categories),
            entry.description,
        )
    )


def _single_line(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _safe_filename_part(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in value
    )
    return cleaned or "feed"
