"""Detailed price digests, card batches, and report attachments."""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
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
MAX_REPORT_ROWS: Final = 50_000
MAX_REPORT_BYTES: Final = 8 * 1024 * 1024
MAX_DIGEST_CATEGORIES: Final = 5
MAX_TOP_MOVES_PER_DIRECTION: Final = 3
PLAUSIBLE_RATIO_MIN: Final = Decimal("0.1")
PLAUSIBLE_RATIO_MAX: Final = Decimal(10)
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
FORMULA_PREFIXES: Final = frozenset("=+-@")


def build_digest(
    feed: FeedConfig,
    records: Sequence[PriceNotificationRecord],
) -> WebhookMessage:
    """Build one summary message with stats, top moves, and full reports."""
    if not records:
        raise ValueError("digest requires at least one notification")
    source_title = _single_line(records[0].source_title, 160)
    observed_at = records[0].observed_at
    header = _digest_header(records, source_title, observed_at)
    html_report, csv_report = build_report_files(
        records[0].feed_id,
        records[0].group_id,
        records,
    )
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
        files=(html_report, csv_report),
        components=({"content": header, "type": TEXT_DISPLAY_COMPONENT},),
    )


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
    observed_at: int,
) -> str:
    increases, decreases, unchanged = _direction_counts(records)
    lines = [
        "## Price changes",
        f"**{len(records)}** changes in {source_title}",
        f"{increases:,} increases · {decreases:,} decreases · {unchanged:,} unchanged",
        f"Observed <t:{observed_at}:f>",
    ]
    categories = _category_counts(records)
    if categories:
        rendered = " · ".join(
            f"{name} ({count:,})" for name, count in categories[:MAX_DIGEST_CATEGORIES]
        )
        lines.append(f"Top categories: {rendered}")
    drops, rises, extremes = _top_moves(records)
    if drops:
        lines.append("**Largest drops**")
        lines.extend(
            _move_line(record, previous, current, ratio)
            for record, previous, current, ratio in drops
        )
    if rises:
        lines.append("**Largest rises**")
        lines.extend(
            _move_line(record, previous, current, ratio)
            for record, previous, current, ratio in rises
        )
    if extremes:
        lines.append(
            f"{extremes:,} extreme moves (over 10× either way) are in the report.",
        )
    lines.append("The complete searchable list is attached (HTML and CSV).")
    return _bounded("\n".join(lines), MAX_TEXT_DISPLAY_CHARACTERS)


def _move_line(
    record: PriceNotificationRecord,
    previous: Decimal,
    current: Decimal,
    ratio: Decimal,
) -> str:
    percent = (ratio - 1) * 100
    title = _single_line(record.title, 60).replace("[", "\\[").replace("]", "\\]")
    link = record.link.replace(">", "%3E").replace("\n", "").replace("\r", "")
    return (
        f"- [{title}](<{link}>) {record.previous_price} → {record.current_price} "
        f"({percent:+.1f}%)"
    )


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
        if previous is None or previous <= 0 or current <= 0:
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


def _card_components(
    feed: FeedConfig,
    record: PriceNotificationRecord,
) -> tuple[JSONValue, ...]:
    entry = replace(record.entry, image_url=None)
    payload = build_components_v2_payload(feed, entry, record.source_title)
    components = payload.get("components")
    return tuple(components) if isinstance(components, list) else ()


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
        write_row=lambda record: writer.writerow(_csv_row(feed_id, record)),
        write_truncation=lambda omitted: writer.writerow(
            ("", "", f"TRUNCATED: {omitted} rows omitted", "", "", "", "", ""),
        ),
    )
    del written
    return buffer.getvalue()


def _html_report(
    feed_id: str,
    records: Sequence[PriceNotificationRecord],
) -> str:
    buffer = io.StringIO()
    buffer.write(_HTML_PREFIX.replace("{feed}", escape(feed_id)))
    written = _write_bounded_rows(
        buffer,
        records,
        write_row=lambda record: buffer.write(_html_row(record)),
        write_truncation=lambda omitted: buffer.write(
            f'<tr class="truncated"><td colspan="6">TRUNCATED: '
            f"{omitted} rows omitted</td></tr>",
        ),
    )
    buffer.write(_HTML_SUFFIX)
    return buffer.getvalue().replace("{count}", f"{written:,}")


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


_HTML_PREFIX: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Price changes — {feed}</title>
<style>
:root { color-scheme: light dark; }
body { font: 15px/1.45 system-ui, sans-serif; margin: 0; padding: 16px; }
h1 { font-size: 1.3rem; margin: 0 0 12px; }
.controls { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
input, select { padding: 8px; font: inherit; border: 1px solid #8884; border-radius: 6px; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 720px; }
th, td { padding: 8px 10px; border-bottom: 1px solid #8883; text-align: left; vertical-align: top; }
th { cursor: pointer; position: sticky; top: 0; background: Canvas; }
tr.up td:nth-child(5) { color: #c0392b; }
tr.down td:nth-child(5) { color: #1e8449; }
tr.truncated td { font-weight: 600; text-align: center; }
a { color: inherit; }
.note { opacity: .7; font-size: .9em; }
</style>
</head>
<body>
<h1>Price changes — {feed}</h1>
<div class="controls">
<input id="q" type="search" placeholder="Filter by title or category" aria-label="Filter">
<select id="dir" aria-label="Direction">
<option value="">All changes</option>
<option value="up">Increases</option>
<option value="down">Decreases</option>
<option value="unchanged">Unchanged</option>
</select>
<span class="note">{count} rows</span>
</div>
<div class="table-wrap">
<table id="t">
<thead><tr>
<th data-sort="text">Product</th><th data-sort="text">Category</th>
<th data-sort="text">Previous</th><th data-sort="text">Current</th>
<th data-sort="number">Change</th><th data-sort="text">Direction</th>
</tr></thead>
<tbody>
"""

_HTML_SUFFIX: Final = """</tbody>
</table>
</div>
<script>
const q = document.getElementById('q');
const dir = document.getElementById('dir');
const rows = [...document.querySelectorAll('#t tbody tr[data-direction]')];
function apply() {
  const text = q.value.trim().toLowerCase();
  const direction = dir.value;
  for (const row of rows) {
    const visible = (!text || row.dataset.search.includes(text))
      && (!direction || row.dataset.direction === direction);
    row.hidden = !visible;
  }
}
q.addEventListener('input', apply);
dir.addEventListener('change', apply);
for (const th of document.querySelectorAll('#t th')) {
  th.addEventListener('click', () => {
    const index = [...th.parentElement.children].indexOf(th);
    const numeric = th.dataset.sort === 'number';
    const body = th.closest('table').tBodies[0];
    const sorted = [...body.rows].sort((a, b) => {
      const av = numeric ? parseFloat(a.cells[index].dataset.value || '0')
                         : a.cells[index].textContent.trim().toLowerCase();
      const bv = numeric ? parseFloat(b.cells[index].dataset.value || '0')
                         : b.cells[index].textContent.trim().toLowerCase();
      return av < bv ? -1 : av > bv ? 1 : 0;
    });
    if (th.dataset.asc === 'true') sorted.reverse();
    th.dataset.asc = th.dataset.asc === 'true' ? 'false' : 'true';
    for (const row of sorted) body.appendChild(row);
  });
}
</script>
</body>
</html>
"""


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
