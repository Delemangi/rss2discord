"""Detailed price digest and report-file delivery for the notification outbox."""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import Final

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.discord.components import (
    MAX_TEXT_DISPLAY_CHARACTERS,
    TEXT_DISPLAY_COMPONENT,
    JSONValue,
    build_components_v2_payload,
)
from rss2discord.discord.message import ReportFile, WebhookMessage
from rss2discord.models import EntryData
from rss2discord.price_notifications import (
    PriceNotificationDeliveryResult,
    PriceNotificationRecord,
)
from rss2discord.transports.price_monitor.contracts import (
    PriceChangeDeliveryDependencies,
)

INLINE_CARD_LIMIT: Final = 25
DIGEST_CARD_LIMIT: Final = 3
MAX_REPORT_ROWS: Final = 50_000
MAX_REPORT_BYTES: Final = 8 * 1024 * 1024
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
MARKDOWN_HEADER: Final = "| Title | Previous | Current | Currency | Observed |"
MARKDOWN_DIVIDER: Final = "| --- | --- | --- | --- | --- |"
FORMULA_PREFIXES: Final = frozenset("=+-@")


def build_digest(
    feed: FeedConfig,
    records: Sequence[PriceNotificationRecord],
) -> WebhookMessage:
    """Build one bounded message with a header, up to three cards, and reports."""
    if not records:
        raise ValueError("digest requires at least one notification")
    source_title = _single_line(records[0].source_title, 160)
    observed_at = records[0].observed_at
    header = (
        "## Price changes\n"
        f"**{len(records)}** price changes in {_single_line(source_title, 160)}\n"
        f"Observed <t:{observed_at}:f>\n"
        "The complete list is attached."
    )
    card_components = _digest_cards(feed, records, len(header))
    feed_id = records[0].feed_id
    group_id = records[0].group_id
    markdown, csv_report = build_report_files(feed_id, group_id, records)
    return WebhookMessage(
        feed=feed,
        entry=EntryData(
            title="Price changes",
            link=feed.url,
            description=header,
            author="",
            timestamp=None,
        ),
        source_title=source_title,
        files=(markdown, csv_report),
        components=(
            {"content": header, "type": TEXT_DISPLAY_COMPONENT},
            *card_components,
        ),
    )


def build_report_files(
    feed_id: str,
    group_id: str,
    records: Sequence[PriceNotificationRecord],
) -> tuple[ReportFile, ReportFile]:
    """Render the complete change list as Markdown and CSV attachments."""
    suffix = f"{_safe_filename_part(feed_id)}-{group_id[:8]}"
    markdown = _markdown_report(records)
    csv_report = _csv_report(feed_id, records)
    return (
        ReportFile(
            filename=f"price-changes-{suffix}.md",
            content=markdown.encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        ),
        ReportFile(
            filename=f"price-changes-{suffix}.csv",
            content=csv_report.encode("utf-8"),
            content_type="text/csv; charset=utf-8",
        ),
    )


def deliver_price_notifications(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
    feed_id: str,
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
            else _deliver_digest(dependencies, feed, records)
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
        if (
            delay_before_next
            and dependencies.delivery.delay_between_posts > 0
            and (
                not dependencies.delivery.sleep(
                    dependencies.delivery.delay_between_posts,
                )
            )
        ):
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
) -> PriceNotificationDeliveryResult:
    if dependencies.delivery.is_shutdown_requested():
        return PriceNotificationDeliveryResult.INTERRUPTED
    result = dependencies.sender.send(
        build_digest(feed, records),
        dependencies.delivery.sleep,
    )
    if result is DiscordDeliveryResult.DELIVERED:
        _mark_sent(dependencies, (record.notification_id for record in records))
        return PriceNotificationDeliveryResult.DRAINED
    if result is DiscordDeliveryResult.INTERRUPTED:
        return PriceNotificationDeliveryResult.INTERRUPTED
    _record_attempt(dependencies, records)
    return PriceNotificationDeliveryResult.FAILED


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


def _digest_cards(
    feed: FeedConfig,
    records: Sequence[PriceNotificationRecord],
    header_characters: int,
) -> tuple[JSONValue, ...]:
    selected = tuple(records[:DIGEST_CARD_LIMIT])
    fixed_cards = tuple(_card_components(feed, record, "") for record in selected)
    fixed_characters = sum(_text_characters(card) for card in fixed_cards)
    budget = MAX_TEXT_DISPLAY_CHARACTERS - header_characters - fixed_characters
    while fixed_cards and budget < 0:
        fixed_cards = fixed_cards[:-1]
        fixed_characters = sum(_text_characters(card) for card in fixed_cards)
        budget = MAX_TEXT_DISPLAY_CHARACTERS - header_characters - fixed_characters
    if not fixed_cards:
        return ()
    description_limit = max(0, budget // len(fixed_cards))
    return tuple(
        component
        for record in selected[: len(fixed_cards)]
        for component in _card_components(
            feed,
            record,
            record.entry.description[:description_limit],
        )
    )


def _card_components(
    feed: FeedConfig,
    record: PriceNotificationRecord,
    description: str,
) -> tuple[JSONValue, ...]:
    entry = replace(record.entry, description=description, image_url=None)
    payload = build_components_v2_payload(feed, entry, record.source_title)
    components = payload.get("components")
    return tuple(components) if isinstance(components, list) else ()


def _text_characters(components: Iterable[JSONValue]) -> int:
    total = 0
    for component in components:
        if isinstance(component, dict):
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


def _csv_report(
    feed_id: str,
    records: Sequence[PriceNotificationRecord],
) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    written = _write_bounded_rows(
        buffer,
        records,
        write_row=lambda record: writer.writerow(
            _csv_row(feed_id, record),
        ),
        write_truncation=lambda omitted: writer.writerow(
            ("", "", f"TRUNCATED: {omitted} rows omitted", "", "", "", "", ""),
        ),
    )
    del written
    return buffer.getvalue()


def _markdown_report(records: Sequence[PriceNotificationRecord]) -> str:
    buffer = io.StringIO()
    buffer.write(f"{MARKDOWN_HEADER}\n{MARKDOWN_DIVIDER}\n")
    _write_bounded_rows(
        buffer,
        records,
        write_row=lambda record: buffer.write(f"{_markdown_row(record)}\n"),
        write_truncation=lambda omitted: buffer.write(
            f"| **TRUNCATED** | {omitted} rows omitted | | | |\n",
        ),
    )
    return buffer.getvalue()


def _write_bounded_rows(
    buffer: io.StringIO,
    records: Sequence[PriceNotificationRecord],
    *,
    write_row: Callable[[PriceNotificationRecord], object],
    write_truncation: Callable[[int], object],
) -> int:
    written = 0
    for record in records:
        if written >= MAX_REPORT_ROWS:
            break
        start = buffer.tell()
        write_row(record)
        if buffer.tell() > MAX_REPORT_BYTES:
            buffer.seek(start)
            buffer.truncate()
            break
        written += 1
    omitted = len(records) - written
    if omitted:
        write_truncation(omitted)
    return written


def _csv_row(feed_id: str, record: PriceNotificationRecord) -> tuple[str, ...]:
    return tuple(
        _csv_safe(value)
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


def _csv_safe(value: str) -> str:
    return f"'{value}" if value[:1] in FORMULA_PREFIXES else value


def _markdown_row(record: PriceNotificationRecord) -> str:
    title = _markdown_cell(record.title, 300)
    link = record.link.replace(">", "%3E").replace("\n", "").replace("\r", "")
    return (
        f"| [{title}](<{link}>) | "
        f"{_markdown_cell(record.previous_price or '', 64)} | "
        f"{_markdown_cell(record.current_price, 64)} | "
        f"{_markdown_cell(record.currency, 16)} | "
        f"{_observed_text(record.observed_at)} |"
    )


def _markdown_cell(value: str, limit: int) -> str:
    return (
        _single_line(value, limit)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _observed_text(observed_at: int) -> str:
    return datetime.fromtimestamp(observed_at, UTC).isoformat()


def _single_line(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _safe_filename_part(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in value
    )
    return cleaned or "feed"
