"""Shared bounded-row and HTML shell utilities for report attachments."""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from html import escape
from typing import Final

MAX_REPORT_ROWS: Final = 50_000
# Four million characters is at most 16 MiB of UTF-8, below Discord's 20 MiB
# per-file upload limit.
MAX_REPORT_CHARACTERS: Final = 4_000_000
FORMULA_PREFIXES: Final = frozenset("=+-@")


def csv_safe(value: str) -> str:
    """Prefix spreadsheet formulas so exported cells stay inert."""
    return f"'{value}" if value[:1] in FORMULA_PREFIXES else value


def write_bounded_rows[ItemT](
    buffer: io.StringIO,
    items: Sequence[ItemT],
    *,
    write_row: Callable[[ItemT], object],
    write_truncation: Callable[[int], object],
) -> int:
    """Write rows until the row/character cap, then one explicit note."""
    written = 0
    for item in items:
        if written >= MAX_REPORT_ROWS:
            break
        start = buffer.tell()
        write_row(item)
        if buffer.tell() > MAX_REPORT_CHARACTERS:
            buffer.seek(start)
            buffer.truncate()
            break
        written += 1
    omitted = len(items) - written
    if omitted:
        write_truncation(omitted)
    return written


def html_shell(
    feed_id: str,
    title: str,
    headers: Sequence[tuple[str, str]],
    body: str,
    count: int,
) -> str:
    """Render the common searchable, sortable, mobile-friendly HTML shell."""
    safe_feed = escape(feed_id)
    safe_title = escape(title)
    direction_filter = any(label == "Direction" for label, _sort in headers)
    header_cells = "".join(
        f'<th data-sort="{escape(sort, quote=True)}">{escape(label)}</th>'
        for label, sort in headers
    )
    direction_control = (
        '<select id="dir" aria-label="Direction">'
        '<option value="">All changes</option>'
        '<option value="up">Increases</option>'
        '<option value="down">Decreases</option>'
        '<option value="unchanged">Unchanged</option>'
        "</select>"
        if direction_filter
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title} — {safe_feed}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font: 15px/1.45 system-ui, sans-serif; margin: 0; padding: 16px; }}
h1 {{ font-size: 1.3rem; margin: 0 0 12px; }}
.controls {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }}
input, select {{ padding: 8px; font: inherit; border: 1px solid #8884; border-radius: 6px; }}
input {{ flex: 1 1 260px; }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; min-width: 760px; }}
th, td {{ padding: 8px 10px; border-bottom: 1px solid #8883; text-align: left; vertical-align: top; }}
th {{ cursor: pointer; position: sticky; top: 0; background: Canvas; }}
tr.up td:nth-child(5) {{ color: #c0392b; }}
tr.down td:nth-child(5) {{ color: #1e8449; }}
tr.truncated td {{ font-weight: 600; text-align: center; }}
a {{ color: inherit; }}
.note {{ opacity: .7; font-size: .9em; }}
</style>
</head>
<body>
<h1>{safe_title} — {safe_feed}</h1>
<div class="controls">
<input id="q" type="search" placeholder="Filter by title, author, or category" aria-label="Search">
{direction_control}
<span class="note">{count:,} rows</span>
</div>
<div class="table-wrap">
<table id="t">
<thead><tr>{header_cells}</tr></thead>
<tbody>
{body}</tbody>
</table>
</div>
<script>
const q = document.getElementById('q');
const dir = document.getElementById('dir');
const rows = [...document.querySelectorAll('#t tbody tr[data-search]')];
function apply() {{
  const text = q.value.trim().toLowerCase();
  const direction = dir ? dir.value : '';
  for (const row of rows) {{
    const visible = (!text || row.dataset.search.includes(text))
      && (!direction || row.dataset.direction === direction);
    row.hidden = !visible;
  }}
}}
q.addEventListener('input', apply);
if (dir) dir.addEventListener('change', apply);
for (const th of document.querySelectorAll('#t th')) {{
  th.addEventListener('click', () => {{
    const index = [...th.parentElement.children].indexOf(th);
    const numeric = th.dataset.sort === 'number';
    const body = th.closest('table').tBodies[0];
    const sorted = [...body.rows].sort((a, b) => {{
      const av = numeric ? parseFloat(a.cells[index].dataset.value || '0')
                         : a.cells[index].textContent.trim().toLowerCase();
      const bv = numeric ? parseFloat(b.cells[index].dataset.value || '0')
                         : b.cells[index].textContent.trim().toLowerCase();
      return av < bv ? -1 : av > bv ? 1 : 0;
    }});
    if (th.dataset.asc === 'true') sorted.reverse();
    th.dataset.asc = th.dataset.asc === 'true' ? 'false' : 'true';
    for (const row of sorted) body.appendChild(row);
  }});
}}
</script>
</body>
</html>
"""
