"""Discovery digest: compact cards plus searchable HTML/CSV attachments."""

from __future__ import annotations

import csv
import io
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
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
from rss2discord.models import EntryData

INLINE_ENTRY_LIMIT: Final = 25
MAX_DIGEST_CARDS: Final = 3
MAX_DIGEST_CATEGORIES: Final = 5
MAX_REPORT_ROWS: Final = 50_000
MAX_REPORT_BYTES: Final = 8 * 1024 * 1024
FORMULA_PREFIXES: Final = frozenset("=+-@")
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
    buffer = io.StringIO()
    buffer.write(_HTML_PREFIX.replace("{feed}", escape(feed_id)))
    written = _write_bounded_rows(
        buffer,
        entries,
        write_row=lambda item: buffer.write(_html_row(item)),
        write_truncation=lambda omitted: buffer.write(
            f'<tr class="truncated"><td colspan="5">TRUNCATED: '
            f"{omitted:,} rows omitted</td></tr>",
        ),
    )
    buffer.write(_HTML_SUFFIX)
    return buffer.getvalue().replace("{count}", f"{written:,}")


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
    _write_bounded_rows(
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
        _csv_safe(value)
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


def _csv_safe(value: str) -> str:
    return f"'{value}" if value[:1] in FORMULA_PREFIXES else value


def _write_bounded_rows(
    buffer: io.StringIO,
    entries: Sequence[tuple[str, EntryData]],
    *,
    write_row: Callable[[tuple[str, EntryData]], object],
    write_truncation: Callable[[int], object],
) -> int:
    written = 0
    for item in entries:
        if written >= MAX_REPORT_ROWS:
            break
        start = buffer.tell()
        write_row(item)
        if buffer.tell() > MAX_REPORT_BYTES:
            buffer.seek(start)
            buffer.truncate()
            break
        written += 1
    omitted = len(entries) - written
    if omitted:
        write_truncation(omitted)
    return written


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


_HTML_PREFIX: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>New items — {feed}</title>
<style>
:root { color-scheme: light dark; }
body { font: 15px/1.45 system-ui, sans-serif; margin: 0; padding: 16px; }
h1 { font-size: 1.3rem; margin: 0 0 12px; }
.controls { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
input { padding: 8px; font: inherit; border: 1px solid #8884; border-radius: 6px; flex: 1 1 260px; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 760px; }
th, td { padding: 8px 10px; border-bottom: 1px solid #8883; text-align: left; vertical-align: top; }
th { cursor: pointer; position: sticky; top: 0; background: Canvas; }
tr.truncated td { font-weight: 600; text-align: center; }
a { color: inherit; }
.note { opacity: .7; font-size: .9em; }
</style>
</head>
<body>
<h1>New items — {feed}</h1>
<div class="controls">
<input id="q" type="search" placeholder="Search title, author, categories" aria-label="Search">
<span class="note">{count} rows</span>
</div>
<div class="table-wrap">
<table id="t">
<thead><tr>
<th data-sort="text">Title</th><th data-sort="text">Author</th>
<th data-sort="text">Published</th><th data-sort="text">Categories</th>
<th data-sort="text">Link</th>
</tr></thead>
<tbody>
"""

_HTML_SUFFIX: Final = """</tbody>
</table>
</div>
<script>
const q = document.getElementById('q');
const rows = [...document.querySelectorAll('#t tbody tr[data-search]')];
function apply() {
  const text = q.value.trim().toLowerCase();
  for (const row of rows) {
    row.hidden = !!text && !row.dataset.search.includes(text);
  }
}
q.addEventListener('input', apply);
for (const th of document.querySelectorAll('#t th')) {
  th.addEventListener('click', () => {
    const index = [...th.parentElement.children].indexOf(th);
    const body = th.closest('table').tBodies[0];
    const sorted = [...body.rows].sort((a, b) => {
      const av = a.cells[index].textContent.trim().toLowerCase();
      const bv = b.cells[index].textContent.trim().toLowerCase();
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
