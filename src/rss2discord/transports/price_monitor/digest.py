"""Detailed price digests, card batches, and report attachments."""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
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
MAX_CATEGORY_NAME_CHARACTERS: Final = 40
MAX_BRIEF_CHARACTERS: Final = 3_900

type _Section = tuple[str, int, list[str]]
type _Movement = tuple[PriceNotificationRecord, Decimal, Decimal, Decimal]
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
    (
        drop_items,
        rise_items,
        drops,
        rises,
        extremes,
        unchanged,
        new,
        restocked,
    ) = _split_changes(records)
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
            (restocked, "restocked"),
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

    sections: list[_Section] = []
    if drop_items:
        sections.append(
            ("Largest drops", drops, [_move_line(item) for item in drop_items]),
        )
    if rise_items:
        sections.append(
            ("Largest rises", rises, [_move_line(item) for item in rise_items]),
        )

    message = _brief_message(
        feed,
        source_title=source_title,
        prefix=prefix,
        sections=sections,
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
    sections: list[_Section],
    suffix: list[str],
    files: tuple[ReportFile, ...],
) -> WebhookMessage:
    text = _brief_text(prefix, sections, suffix)
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
    sections: list[_Section],
    suffix: list[str],
) -> str:
    while True:
        text = "\n".join([prefix, *_section_lines(sections), *suffix])
        if len(text) <= MAX_BRIEF_CHARACTERS or not _trim_last_section(sections):
            return _bounded(text, MAX_TEXT_DISPLAY_CHARACTERS)


def _section_lines(sections: list[_Section]) -> list[str]:
    lines: list[str] = []
    for label, total, rows in sections:
        if not rows:
            continue
        lines.append(_section_heading(label, len(rows), total))
        lines.extend(rows)
    return lines


def _trim_last_section(sections: list[_Section]) -> bool:
    for index in range(len(sections) - 1, -1, -1):
        _label, _total, rows = sections[index]
        if not rows:
            continue
        rows.pop()
        if not rows:
            sections.pop(index)
        return True
    return False


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
        category = _single_line(categories[0], 200) if categories else ""
        if not category:
            category = "Uncategorized"
        counts[category] += 1
    if not counts or set(counts) <= {"Uncategorized"}:
        return None
    items = counts.most_common(MAX_DIGEST_CATEGORIES)
    return "Categories: " + ", ".join(
        f"{_truncate_words(name, MAX_CATEGORY_NAME_CHARACTERS)} ({count:,})"
        for name, count in items
    )


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
    list[_Movement],
    list[_Movement],
    int,
    int,
    int,
    int,
    int,
    int,
]:
    """Return top drops, top rises, and disjoint counts for the summary.

    A restock counts once under ``restocked`` even when it also carries a real
    price move. Such a move stays eligible for the drops/rises lists, but a
    restock whose price is unchanged never looks like a price move.
    """
    tally = _ChangeTally()
    for record in records:
        tally.add(record)
    return tally.result()


@dataclass(slots=True)
class _ChangeTally:
    """Disjoint per-scan counts plus the candidate movement lists."""

    drops: list[_Movement] = field(default_factory=list)
    rises: list[_Movement] = field(default_factory=list)
    drop_count: int = 0
    rise_count: int = 0
    unchanged_count: int = 0
    new_count: int = 0
    extreme_count: int = 0
    restocked_count: int = 0

    def add(self, record: PriceNotificationRecord) -> None:
        if record.entry.restocked:
            self._add_restock(record)
        else:
            self._add_price_move(record)

    def _add_restock(self, record: PriceNotificationRecord) -> None:
        self.restocked_count += 1
        previous = record.previous_amount
        current = record.current_amount
        if previous is None or previous <= 0 or current == previous:
            return
        ratio = current / previous
        if ratio < PLAUSIBLE_RATIO_MIN or ratio > PLAUSIBLE_RATIO_MAX:
            self.extreme_count += 1
        elif ratio < 1:
            self.drops.append((record, previous, current, ratio))
        else:
            self.rises.append((record, previous, current, ratio))

    def _add_price_move(self, record: PriceNotificationRecord) -> None:
        previous = record.previous_amount
        current = record.current_amount
        if previous is None or previous <= 0:
            self.new_count += 1
            return
        if current == previous:
            self.unchanged_count += 1
            return
        ratio = current / previous
        extreme = ratio < PLAUSIBLE_RATIO_MIN or ratio > PLAUSIBLE_RATIO_MAX
        if extreme:
            self.extreme_count += 1
        if ratio < 1:
            self.drop_count += 1
            if not extreme:
                self.drops.append((record, previous, current, ratio))
        else:
            self.rise_count += 1
            if not extreme:
                self.rises.append((record, previous, current, ratio))

    def result(
        self,
    ) -> tuple[list[_Movement], list[_Movement], int, int, int, int, int, int]:
        self.drops.sort(key=lambda item: (item[3], -(item[2] - item[1]).copy_abs()))
        self.rises.sort(key=lambda item: (-item[3], -(item[2] - item[1]).copy_abs()))
        return (
            self.drops[:MAX_TOP_DROPS],
            self.rises[:MAX_TOP_RISES],
            self.drop_count,
            self.rise_count,
            self.extreme_count,
            self.unchanged_count,
            self.new_count,
            self.restocked_count,
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
