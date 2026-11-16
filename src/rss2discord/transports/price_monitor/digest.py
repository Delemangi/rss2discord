"""Detailed price digests, card batches, and report attachments."""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
from typing import Final

from rss2discord.configuration import FeedConfig
from rss2discord.discord.attachments import ReportFile
from rss2discord.discord.components import (
    MAX_TEXT_DISPLAY_CHARACTERS,
    TEXT_DISPLAY_COMPONENT,
)
from rss2discord.discord.message import WebhookMessage
from rss2discord.discord.report import (
    csv_safe,
    html_shell,
    write_bounded_rows,
)
from rss2discord.models import EntryData
from rss2discord.price_notifications import PriceNotificationRecord

MAX_TOP_DROPS: Final = 5
MAX_TOP_RISES: Final = 5
MAX_DIGEST_CATEGORIES: Final = 5
MAX_BRIEF_CHARACTERS: Final = 3_900
PLAUSIBLE_RATIO_MIN: Final = Decimal("0.1")
PLAUSIBLE_RATIO_MAX: Final = Decimal(10)
ATTACHMENT_NOTE: Final = "The complete searchable list is attached (HTML and CSV)."
CSV_HEADER: Final = (
    "feed",
    "product_id",
    "title",
    "link",
    "previous",
    "current",
    "currency",
    "observed_at",
)
PRICE_HEADERS: Final = (
    ("Product", "text"),
    ("Category", "text"),
    ("Previous", "text"),
    ("Current", "text"),
    ("Change", "number"),
    ("Direction", "text"),
)


def build_digest_messages(
    feed: FeedConfig,
    records: Sequence[PriceNotificationRecord],
    *,
    additional_pending: int = 0,
) -> tuple[WebhookMessage, ...]:
    """Build a single brief digest message plus the complete reports."""
    if not records:
        raise ValueError("digest requires at least one notification")
    source_title = _single_line(records[0].source_title, 160)
    drop_items, rise_items, drops, rises, extremes, unchanged, new = _split_changes(
        records,
    )
    files = build_report_files(
        records[0].feed_id,
        records[0].group_id,
        records,
    )

    extreme_note = _extreme_note(extremes)
    pending_note = (
        f"{additional_pending:,} additional changes are pending confirmation "
        "and will follow in later scans."
        if additional_pending
        else None
    )
    suffix = [
        *_optional(extreme_note),
        *_optional(pending_note),
        ATTACHMENT_NOTE,
    ]

    summary_parts = [
        f"{count:,} {label}"
        for count, label in [
            (drops, "dropped"),
            (rises, "rose"),
            (unchanged, "unchanged"),
            (new, "new"),
            (extremes, "extreme move" if extremes == 1 else "extreme moves"),
        ]
        if count
    ]
    change_label = "change" if len(records) == 1 else "changes"
    prefix_lines = [
        f"## Price changes — {source_title}",
        f"**{len(records):,}** {change_label}: {', '.join(summary_parts)}.",
        f"Prices observed <t:{records[0].observed_at}:f>",
    ]
    category_line = _category_line(records)
    if category_line is not None:
        prefix_lines.append(category_line)
    prefix = "\n".join(prefix_lines)

    rows: list[str] = []
    if drop_items:
        rows.append(_section_heading("Largest drops", len(drop_items), drops))
        rows.extend(_move_line(item) for item in drop_items)
    if rise_items:
        rows.append(_section_heading("Largest rises", len(rise_items), rises))
        rows.extend(_move_line(item) for item in rise_items)

    message = _brief_message(
        feed,
        source_title=source_title,
        prefix=prefix,
        rows=rows,
        suffix=suffix,
        files=files,
    )
    return (message,)


def _optional(value: str | None) -> list[str]:
    return [] if value is None else [value]


def _section_heading(label: str, shown: int, total: int) -> str:
    if shown >= total:
        return f"**{label}**"
    return f"**{label}** — top {shown} of {total:,}"


def _extreme_note(extremes: int) -> str | None:
    if not extremes:
        return None
    if extremes == 1:
        return "1 extreme move (over 10× either way) is in the report."
    return f"{extremes:,} extreme moves (over 10× either way) are in the report."


def _brief_message(
    feed: FeedConfig,
    *,
    source_title: str,
    prefix: str,
    rows: list[str],
    suffix: list[str],
    files: tuple[ReportFile, ...],
) -> WebhookMessage:
    text = _brief_text(prefix, rows, suffix)
    return WebhookMessage(
        feed=feed,
        entry=EntryData(
            title="Price changes",
            link=feed.url,
            description=text,
            author="",
            timestamp=None,
        ),
        source_title=source_title,
        files=files,
        components=({"content": text, "type": TEXT_DISPLAY_COMPONENT},),
    )


def _brief_text(
    prefix: str,
    rows: list[str],
    suffix: list[str],
) -> str:
    while True:
        parts = [prefix, *rows, *suffix]
        text = "\n".join(parts)
        if len(text) <= MAX_BRIEF_CHARACTERS or not rows:
            return _bounded(text, MAX_TEXT_DISPLAY_CHARACTERS)
        rows.pop()


def build_report_files(
    feed_id: str,
    group_id: str,
    records: Sequence[PriceNotificationRecord],
) -> tuple[ReportFile, ReportFile]:
    """Render the complete change list as a searchable HTML report and CSV."""
    suffix = f"{_safe_filename_part(feed_id)}-{group_id[:8]}"
    return (
        ReportFile(
            filename=f"price-changes-{suffix}.html",
            content=_html_report(feed_id, records).encode("utf-8"),
            content_type="text/html; charset=utf-8",
        ),
        ReportFile(
            filename=f"price-changes-{suffix}.csv",
            content=_csv_report(feed_id, records).encode("utf-8"),
            content_type="text/csv; charset=utf-8",
        ),
    )


def _category_line(records: Sequence[PriceNotificationRecord]) -> str | None:
    counts: Counter[str] = Counter()
    for record in records:
        categories = record.entry.categories
        category = categories[0] if categories else "Uncategorized"
        if not category.strip():
            category = "Uncategorized"
        counts[category] += 1
    if not counts or set(counts) <= {"Uncategorized"}:
        return None
    items = counts.most_common(MAX_DIGEST_CATEGORIES)
    return "Categories: " + ", ".join(f"{name} ({count:,})" for name, count in items)


def _move_line(
    item: tuple[PriceNotificationRecord, Decimal, Decimal, Decimal],
) -> str:
    record, _previous, _current, ratio = item
    percent = (ratio - 1) * 100
    previous_price = record.previous_price or "—"
    title = _truncate_words(_single_line(record.title, 200), 70)
    title = title.replace("[", "\\[").replace("]", "\\]")
    link = (
        record.link.replace(")", "%29")
        .replace(" ", "%20")
        .replace("\n", "")
        .replace("\r", "")
    )
    return (
        f"{previous_price} → {record.current_price} ({percent:+.1f}%) [{title}]({link})"
    )


def _split_changes(
    records: Sequence[PriceNotificationRecord],
) -> tuple[
    list[tuple[PriceNotificationRecord, Decimal, Decimal, Decimal]],
    list[tuple[PriceNotificationRecord, Decimal, Decimal, Decimal]],
    int,
    int,
    int,
    int,
    int,
]:
    """Return top drops, top rises, and counts for summary."""
    drop_count = rise_count = unchanged_count = new_count = extreme_count = 0
    drops: list[tuple[PriceNotificationRecord, Decimal, Decimal, Decimal]] = []
    rises: list[tuple[PriceNotificationRecord, Decimal, Decimal, Decimal]] = []

    for record in records:
        previous = record.previous_amount
        current = record.current_amount
        if previous is None or previous <= 0:
            new_count += 1
            continue
        if current == previous:
            unchanged_count += 1
            continue
        ratio = current / previous
        if ratio < PLAUSIBLE_RATIO_MIN or ratio > PLAUSIBLE_RATIO_MAX:
            extreme_count += 1
            continue
        if ratio < 1:
            drops.append((record, previous, current, ratio))
            drop_count += 1
        else:
            rises.append((record, previous, current, ratio))
            rise_count += 1

    drops.sort(key=lambda item: (item[3], -(item[2] - item[1]).copy_abs()))
    rises.sort(key=lambda item: (-item[3], -(item[2] - item[1]).copy_abs()))
    return (
        drops[:MAX_TOP_DROPS],
        rises[:MAX_TOP_RISES],
        drop_count,
        rise_count,
        extreme_count,
        unchanged_count,
        new_count,
    )


def _csv_report(
    feed_id: str,
    records: Sequence[PriceNotificationRecord],
) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    write_bounded_rows(
        buffer,
        records,
        write_row=lambda record: writer.writerow(_csv_row(feed_id, record)),
        write_truncation=lambda omitted: writer.writerow(
            ("", "", f"TRUNCATED: {omitted} rows omitted", "", "", "", "", ""),
        ),
    )
    return buffer.getvalue()


def _html_report(
    feed_id: str,
    records: Sequence[PriceNotificationRecord],
) -> str:
    body = io.StringIO()
    written = write_bounded_rows(
        body,
        records,
        write_row=lambda record: body.write(_html_row(record)),
        write_truncation=lambda omitted: body.write(
            f'<tr class="truncated"><td colspan="6">TRUNCATED: '
            f"{omitted} rows omitted</td></tr>",
        ),
    )
    return html_shell(feed_id, "Price changes", PRICE_HEADERS, body.getvalue(), written)


def _html_row(record: PriceNotificationRecord) -> str:
    previous = escape(record.previous_price or "")
    current = escape(record.current_price)
    category = escape(record.entry.categories[0] if record.entry.categories else "")
    title = escape(_single_line(record.title, 200))
    link = escape(record.link, quote=True)
    direction = "unchanged"
    percent = ""
    if record.previous_amount is not None and record.previous_amount > 0:
        ratio = record.current_amount / record.previous_amount
        if ratio > 1:
            direction = "up"
            percent = f"{(ratio - 1) * 100:+.1f}%"
        elif ratio < 1:
            direction = "down"
            percent = f"{(ratio - 1) * 100:+.1f}%"
    return (
        f'<tr data-direction="{direction}" data-search="'
        f"{escape(_single_line(record.title, 200).lower(), quote=True)} "
        f'{escape(category.lower(), quote=True)}">'
        f'<td><a href="{link}" target="_blank" rel="noreferrer">{title}</a></td>'
        f"<td>{category}</td><td>{previous}</td><td>{current}</td>"
        f'<td data-value="{percent}">{percent}</td><td>{direction}</td></tr>\n'
    )


def _csv_row(feed_id: str, record: PriceNotificationRecord) -> tuple[str, ...]:
    return tuple(
        csv_safe(value)
        for value in (
            feed_id,
            record.product_id,
            record.title,
            record.link,
            record.previous_price or "",
            record.current_price,
            record.currency,
            _observed_text(record.observed_at),
        )
    )


def _observed_text(observed_at: int) -> str:
    return datetime.fromtimestamp(observed_at, UTC).isoformat()


def _single_line(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _truncate_words(value: str, limit: int) -> str:
    """Truncate at a word boundary, falling back to a hard limit."""
    if len(value) <= limit:
        return value
    truncated = value[:limit]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        return truncated[:last_space] + "…"
    return truncated[:-1] + "…"


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _safe_filename_part(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in ".-_" else "-"
        for character in value
    )
    return cleaned or "feed"
