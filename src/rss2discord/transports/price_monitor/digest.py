"""Detailed price digests, card batches, and report attachments."""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from html import escape
from typing import Final

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.discord.components import (
    MAX_TEXT_DISPLAY_CHARACTERS,
    TEXT_DISPLAY_COMPONENT,
)
from rss2discord.discord.message import ReportFile, WebhookMessage
from rss2discord.discord.report import (
    csv_safe,
    html_shell,
    write_bounded_rows,
)
from rss2discord.models import EntryData
from rss2discord.price_notifications import (
    PriceNotificationDeliveryResult,
    PriceNotificationRecord,
)
from rss2discord.transports.price_monitor.contracts import (
    PriceChangeDeliveryDependencies,
)

INLINE_CARD_LIMIT: Final = 25
MAX_DIGEST_CATEGORIES: Final = 5
MAX_TOP_MOVES_PER_DIRECTION: Final = 20
MAX_BRIEF_CHARACTERS: Final = 3_900
CATEGORY_LABEL_WIDTH: Final = 22
CATEGORY_BAR_WIDTH: Final = 14
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
    """Build one or two dense brief messages plus the complete reports."""
    if not records:
        raise ValueError("digest requires at least one notification")
    source_title = _single_line(records[0].source_title, 160)
    header = _digest_header(records, source_title)
    drops, rises, extremes = _top_moves(records)
    files = build_report_files(
        records[0].feed_id,
        records[0].group_id,
        records,
    )
    extreme_note = (
        f"{extremes:,} extreme moves (over 10× either way) are in the report."
        if extremes
        else None
    )
    pending_note = (
        f"{additional_pending:,} additional changes are pending confirmation "
        "and will follow in later scans."
        if additional_pending
        else None
    )
    final_suffix = [
        *_optional(extreme_note),
        *_optional(pending_note),
        ATTACHMENT_NOTE,
    ]
    drop_rows = [_move_line(item) for item in drops]
    rise_rows = [_move_line(item) for item in rises]
    if drops:
        suffix = [] if rises else final_suffix
        messages = [
            _brief_message(
                feed,
                source_title=source_title,
                prefix=header,
                heading="**Largest drops**",
                rows=drop_rows,
                suffix=suffix,
                files=files if not rises else (),
            ),
        ]
        if rises:
            messages.append(
                _brief_message(
                    feed,
                    source_title=source_title,
                    prefix=f"**Largest rises** — {source_title}",
                    heading=None,
                    rows=rise_rows,
                    suffix=final_suffix,
                    files=files,
                ),
            )
        return tuple(messages)
    return (
        _brief_message(
            feed,
            source_title=source_title,
            prefix=header,
            heading="**Largest rises**" if rises else None,
            rows=rise_rows,
            suffix=final_suffix,
            files=files,
        ),
    )


def _optional(value: str | None) -> list[str]:
    return [] if value is None else [value]


def _brief_message(
    feed: FeedConfig,
    *,
    source_title: str,
    prefix: str,
    heading: str | None,
    rows: list[str],
    suffix: list[str],
    files: tuple[ReportFile, ...],
) -> WebhookMessage:
    text = _brief_text(prefix, heading, rows, suffix)
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
    heading: str | None,
    rows: list[str],
    suffix: list[str],
) -> str:
    while True:
        parts = [prefix]
        if heading is not None:
            parts.append(heading)
        parts.extend(rows)
        parts.extend(suffix)
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


def deliver_price_notifications(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
    feed_id: str,
    *,
    additional_pending: int = 0,
) -> PriceNotificationDeliveryResult:
    """Drain every pending group oldest-first, one Discord message at a time."""
    while not dependencies.delivery.is_shutdown_requested():
        group = dependencies.sqlite_retry_policy.execute(
            partial(dependencies.snapshots.load_pending_price_group, feed_id),
        )
        if group is None:
            return PriceNotificationDeliveryResult.DRAINED
        _group_id, records = group
        result = (
            _deliver_inline_cards(dependencies, feed, records)
            if len(records) <= INLINE_CARD_LIMIT
            else _deliver_digest(
                dependencies,
                feed,
                records,
                additional_pending=additional_pending,
            )
        )
        if result is not PriceNotificationDeliveryResult.DRAINED:
            return result
    return PriceNotificationDeliveryResult.INTERRUPTED


def _deliver_inline_cards(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
    records: Sequence[PriceNotificationRecord],
) -> PriceNotificationDeliveryResult:
    delay_before_next = False
    for index, record in enumerate(records):
        if dependencies.delivery.is_shutdown_requested():
            return PriceNotificationDeliveryResult.INTERRUPTED
        if not _wait_before_next(dependencies, delay_before_next):
            return PriceNotificationDeliveryResult.INTERRUPTED
        delay_before_next = False
        result = dependencies.sender.send(
            WebhookMessage(
                feed=feed,
                entry=record.entry,
                source_title=record.source_title,
            ),
            dependencies.delivery.sleep,
        )
        if result is DiscordDeliveryResult.DELIVERED:
            _mark_sent(dependencies, (record.notification_id,))
            delay_before_next = True
            continue
        if result is DiscordDeliveryResult.INTERRUPTED:
            return PriceNotificationDeliveryResult.INTERRUPTED
        _record_attempt(dependencies, records[index:])
        return PriceNotificationDeliveryResult.FAILED
    return PriceNotificationDeliveryResult.DRAINED


def _deliver_digest(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
    records: Sequence[PriceNotificationRecord],
    *,
    additional_pending: int = 0,
) -> PriceNotificationDeliveryResult:
    for index, message in enumerate(
        build_digest_messages(
            feed,
            records,
            additional_pending=additional_pending,
        ),
    ):
        if dependencies.delivery.is_shutdown_requested():
            return PriceNotificationDeliveryResult.INTERRUPTED
        if index and not _wait_before_next(dependencies, True):
            return PriceNotificationDeliveryResult.INTERRUPTED
        result = dependencies.sender.send(message, dependencies.delivery.sleep)
        if result is DiscordDeliveryResult.DELIVERED:
            continue
        if result is DiscordDeliveryResult.INTERRUPTED:
            return PriceNotificationDeliveryResult.INTERRUPTED
        _record_attempt(dependencies, records)
        return PriceNotificationDeliveryResult.FAILED
    _mark_sent(dependencies, (record.notification_id for record in records))
    return PriceNotificationDeliveryResult.DRAINED


def _wait_before_next(
    dependencies: PriceChangeDeliveryDependencies,
    delay_before_next: bool,
) -> bool:
    if not delay_before_next or dependencies.delivery.delay_between_posts <= 0:
        return True
    return dependencies.delivery.sleep(dependencies.delivery.delay_between_posts)


def _mark_sent(
    dependencies: PriceChangeDeliveryDependencies,
    notification_ids: Iterable[int],
) -> None:
    dependencies.sqlite_retry_policy.execute(
        partial(
            dependencies.snapshots.mark_price_notifications_sent,
            tuple(notification_ids),
        ),
    )


def _record_attempt(
    dependencies: PriceChangeDeliveryDependencies,
    records: Iterable[PriceNotificationRecord],
) -> None:
    ids = tuple(record.notification_id for record in records)
    dependencies.sqlite_retry_policy.execute(
        partial(
            dependencies.snapshots.record_price_notification_attempt,
            ids,
            "DiscordDeliveryFailed",
        ),
    )


def _digest_header(
    records: Sequence[PriceNotificationRecord],
    source_title: str,
) -> str:
    increases, decreases, unchanged = _direction_counts(records)
    lines = [
        f"## Price changes — {source_title}",
        (
            f"**{len(records):,}** changes · {increases:,} ↑ · "
            f"{decreases:,} ↓ · {unchanged:,} ="
        ),
        f"Observed <t:{records[0].observed_at}:f>",
    ]
    lines.extend(_category_bars(records))
    return _bounded("\n".join(lines), MAX_TEXT_DISPLAY_CHARACTERS)


def _category_bars(
    records: Sequence[PriceNotificationRecord],
) -> list[str]:
    categories = _category_counts(records)
    if not categories:
        return []
    maximum = max(count for _name, count in categories)
    bars: list[str] = []
    for name, count in categories:
        label = _single_line(name, CATEGORY_LABEL_WIDTH)
        if len(name) > CATEGORY_LABEL_WIDTH:
            label = label[:-1] + "…"
        width = max(1, round(count * CATEGORY_BAR_WIDTH / maximum))
        bars.append(f"{label:<{CATEGORY_LABEL_WIDTH}} {'█' * width} {count:,}")
    return bars


def _move_line(
    item: tuple[PriceNotificationRecord, Decimal, Decimal, Decimal],
) -> str:
    record, previous, current, ratio = item
    percent = (ratio - 1) * 100
    title = _single_line(record.title, 46).replace("[", "\\[").replace("]", "\\]")
    link = (
        record.link.replace(")", "%29")
        .replace(" ", "%20")
        .replace("\n", "")
        .replace("\r", "")
    )
    return (
        f"{_format_amount(previous):>8} → {_format_amount(current):>8}  "
        f"{percent:+7.1f}%  [{title}]({link})"
    )


def _format_amount(value: Decimal) -> str:
    """Render up to two decimals with thousands separators and no trailing zeros."""
    text = f"{value:,.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _direction_counts(
    records: Sequence[PriceNotificationRecord],
) -> tuple[int, int, int]:
    increases = decreases = unchanged = 0
    for record in records:
        previous = record.previous_amount
        current = record.current_amount
        if previous is None or previous <= 0:
            unchanged += 1
        elif current > previous:
            increases += 1
        elif current < previous:
            decreases += 1
        else:
            unchanged += 1
    return increases, decreases, unchanged


def _category_counts(
    records: Sequence[PriceNotificationRecord],
) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for record in records:
        category = (
            record.entry.categories[0] if record.entry.categories else "Uncategorized"
        )
        counter[category] += 1
    return counter.most_common(MAX_DIGEST_CATEGORIES)


def _top_moves(
    records: Sequence[PriceNotificationRecord],
) -> tuple[
    list[tuple[PriceNotificationRecord, Decimal, Decimal, Decimal]],
    list[tuple[PriceNotificationRecord, Decimal, Decimal, Decimal]],
    int,
]:
    drops: list[tuple[PriceNotificationRecord, Decimal, Decimal, Decimal]] = []
    rises: list[tuple[PriceNotificationRecord, Decimal, Decimal, Decimal]] = []
    extremes = 0
    for record in records:
        previous = record.previous_amount
        current = record.current_amount
        if previous is None or previous <= 0:
            continue
        ratio = current / previous
        if ratio < PLAUSIBLE_RATIO_MIN or ratio > PLAUSIBLE_RATIO_MAX:
            extremes += 1
            continue
        if ratio < 1:
            drops.append((record, previous, current, ratio))
        elif ratio > 1:
            rises.append((record, previous, current, ratio))
    drops.sort(key=lambda item: item[3])
    rises.sort(key=lambda item: item[3], reverse=True)
    return (
        drops[:MAX_TOP_MOVES_PER_DIRECTION],
        rises[:MAX_TOP_MOVES_PER_DIRECTION],
        extremes,
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
