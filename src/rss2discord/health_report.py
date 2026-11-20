"""Health summary rendering and optional daily Discord reporting."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Final

import requests

from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import HealthRecord

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARACTERS: Final = 1_900
MAX_PROBLEM_LINES: Final = 12
KNOWN_STATES: Final = frozenset({"healthy", "empty", "failed", "blocked"})
STATE_ORDER: Final = {"failed": 0, "blocked": 1, "empty": 2, "healthy": 4}


@dataclass(frozen=True, slots=True)
class HealthSummary:
    """One point-in-time view of feed health and retained state."""

    total_feeds: int
    healthy: int
    empty: int
    failed: int
    blocked: int
    other: int
    pending_price_notifications: int
    database_bytes: int
    problems: tuple[HealthRecord, ...]


def build_health_summary(store: DeliveryStore, database_path: Path) -> HealthSummary:
    by_feed: dict[str, list[HealthRecord]] = {}
    for record in store.list_health():
        by_feed.setdefault(record.feed_id, []).append(record)
    worst_records = tuple(
        min(
            records,
            key=lambda record: (
                _summary_severity(record),
                record.job_kind,
            ),
        )
        for records in by_feed.values()
    )
    counts = Counter(_summary_state(record) for record in worst_records)
    problems = tuple(
        sorted(
            (record for record in worst_records if _summary_state(record) != "healthy"),
            key=lambda record: (
                _summary_severity(record),
                record.feed_id,
                record.job_kind,
            ),
        ),
    )
    return HealthSummary(
        total_feeds=len(by_feed),
        healthy=counts.get("healthy", 0),
        empty=counts.get("empty", 0),
        failed=counts.get("failed", 0),
        blocked=counts.get("blocked", 0),
        other=sum(
            count for state, count in counts.items() if state not in KNOWN_STATES
        ),
        pending_price_notifications=store.count_pending_price_notifications_total(),
        database_bytes=_database_bytes(database_path),
        problems=problems,
    )


def _is_routine_empty(record: HealthRecord) -> bool:
    """Report a quiet ordinary scan as healthy once the feed has produced items.

    A successful scan with no new entries is normal for a low-churn feed. Only
    empty results that have never produced anything, and empty price monitors,
    need operator attention.
    """
    return (
        record.state == "empty"
        and record.job_kind == "ordinary"
        and record.total_nonempty > 0
    )


def _summary_state(record: HealthRecord) -> str:
    return "healthy" if _is_routine_empty(record) else record.state


def _summary_severity(record: HealthRecord) -> int:
    if _is_routine_empty(record):
        return STATE_ORDER["healthy"]
    return STATE_ORDER.get(record.state, 3)


def format_health_summary(summary: HealthSummary) -> str:
    state_line = (
        f"{summary.total_feeds} feeds: {summary.healthy} healthy, "
        f"{summary.empty} empty, {summary.failed} failed, "
        f"{summary.blocked} blocked"
    )
    if summary.other:
        state_line += f", {summary.other} other"
    lines = [
        "**rss2discord health**",
        state_line,
        f"Pending price notifications: {summary.pending_price_notifications}",
        f"Database: {_format_bytes(summary.database_bytes)}",
    ]
    if summary.problems:
        lines.append("")
        lines.extend(
            _format_problem(record) for record in summary.problems[:MAX_PROBLEM_LINES]
        )
        remaining = len(summary.problems) - MAX_PROBLEM_LINES
        if remaining > 0:
            lines.append(f"… and {remaining} more")
    else:
        lines.extend(("", "All feeds healthy."))
    return "\n".join(lines)[:MAX_SUMMARY_CHARACTERS]


def _format_problem(record: HealthRecord) -> str:
    cause = f" ({record.cause})" if record.cause else ""
    attempted = (
        "never"
        if record.attempted_at is None
        else datetime.fromtimestamp(record.attempted_at, UTC).strftime(
            "%Y-%m-%d %H:%M UTC",
        )
    )
    return (
        f"- `{record.feed_id}` {record.job_kind} **{record.state}**{cause} "
        f"— {attempted}"
    )


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def _database_bytes(database_path: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = database_path.parent / f"{database_path.name}{suffix}"
        try:
            total += candidate.stat().st_size
        except FileNotFoundError:
            continue
    return total


class HealthSummaryReporter:
    """Send a startup summary and repeat it on an interval."""

    def __init__(
        self,
        *,
        webhook: str,
        database_path: Path,
        interval: float,
        sender: Callable[[str], bool] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("Health summary interval must be positive")
        self._webhook = webhook
        self._database_path = database_path
        self._interval = interval
        self._sender = sender or self._post
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = Thread(
            target=self._run,
            name="health-summary",
            daemon=False,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def send_now(self) -> bool:
        with DeliveryStore(self._database_path, read_only=True) as store:
            summary = build_health_summary(store, self._database_path)
        return self._sender(format_health_summary(summary))

    def _run(self) -> None:
        try:
            self.send_now()
        except Exception as error:  # noqa: RUF100  # noqa: BROAD_EXCEPT_OK
            logger.log(
                logging.ERROR,
                "Health summary failed (%s)",
                type(error).__name__,
            )
        while not self._stop.wait(self._interval):
            try:
                self.send_now()
            except Exception as error:  # noqa: RUF100  # noqa: BROAD_EXCEPT_OK
                logger.log(
                    logging.ERROR,
                    "Health summary failed (%s)",
                    type(error).__name__,
                )

    def _post(self, text: str) -> bool:
        response = requests.post(
            self._webhook,
            json={"content": text, "allowed_mentions": {"parse": []}},
            timeout=15,
        )
        if not response.ok:
            logger.warning(
                "Health summary webhook rejected (HTTP %d)",
                response.status_code,
            )
            return False
        return True
