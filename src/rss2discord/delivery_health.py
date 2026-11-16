"""Health-state persistence for delivery jobs."""

from __future__ import annotations

import sqlite3
from typing import Final

from rss2discord.models import (
    HEALTH_REMINDER_SECONDS,
    HealthNotice,
    HealthRecord,
    HealthUpdate,
)

HEALTH_STATES: Final = frozenset({"healthy", "empty", "failed", "blocked"})
FEED_WHERE_CLAUSE: Final = " WHERE feed_id = ?"


class HealthRepository:
    """Persist and read per-feed, per-job health state."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def record_health(
        self,
        update: HealthUpdate,
        *,
        reminder_seconds: int = HEALTH_REMINDER_SECONDS,
    ) -> HealthNotice:
        if update.state not in HEALTH_STATES:
            raise ValueError("unsupported health state")
        if reminder_seconds < 0:
            raise ValueError("reminder_seconds must be non-negative")
        with self._connection:
            row = self._connection.execute(
                "SELECT state, last_notified_at, blocked_until "
                "FROM health_state WHERE feed_id = ? AND job_kind = ?",
                (update.feed_id, update.job_kind),
            ).fetchone()
            previous_state = None if row is None else str(row[0])
            previous_notified = None if row is None else row[1]
            previous_blocked_until = None if row is None else row[2]
            changed = previous_state != update.state
            recovered = previous_state in {
                "failed",
                "blocked",
            } and update.state not in {
                "failed",
                "blocked",
            }
            should_log = self._should_log_health(
                update,
                changed,
                recovered,
                previous_notified,
                reminder_seconds,
            )
            blocked_until = previous_blocked_until
            if update.state == "blocked" and (
                previous_state != "blocked"
                or previous_blocked_until is None
                or previous_blocked_until <= update.attempted_at
            ):
                blocked_until = update.attempted_at + HEALTH_REMINDER_SECONDS
            if update.state != "blocked" and changed:
                blocked_until = None
            if row is None:
                self._insert_health_state(update, blocked_until, should_log)
            else:
                self._connection.execute(
                    "UPDATE health_state SET state = ?, cause = ?, attempted_at = ?, "
                    "success = ?, nonempty = ?, item_count = ?, duration_ms = ?, "
                    "scheduler_lag_ms = ?, transition_at = CASE WHEN ? THEN ? ELSE transition_at END, "
                    "consecutive_failures = CASE WHEN ? THEN 0 ELSE consecutive_failures + 1 END, "
                    "total_attempts = total_attempts + 1, total_successes = total_successes + ?, "
                    "total_nonempty = total_nonempty + ?, total_items = total_items + ?, "
                    "blocked_until = ?, last_notified_at = CASE WHEN ? THEN ? ELSE last_notified_at END, "
                    "last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END, "
                    "last_nonempty_at = CASE WHEN ? THEN ? ELSE last_nonempty_at END "
                    "WHERE feed_id = ? AND job_kind = ?",
                    (
                        update.state,
                        update.cause,
                        update.attempted_at,
                        int(update.success),
                        int(update.nonempty),
                        update.item_count,
                        update.duration_ms,
                        update.scheduler_lag_ms,
                        int(changed),
                        update.attempted_at,
                        int(update.success),
                        int(update.success),
                        int(update.nonempty),
                        update.item_count,
                        blocked_until,
                        int(should_log),
                        update.attempted_at,
                        int(update.success),
                        update.attempted_at,
                        int(update.nonempty),
                        update.attempted_at,
                        update.feed_id,
                        update.job_kind,
                    ),
                )
        return HealthNotice(changed, recovered, should_log, previous_state)

    def _insert_health_state(
        self,
        update: HealthUpdate,
        blocked_until: int | None,
        should_log: bool,
    ) -> None:
        self._connection.execute(
            "INSERT INTO health_state "
            "(feed_id, job_kind, state, cause, attempted_at, success, nonempty, "
            "item_count, duration_ms, scheduler_lag_ms, transition_at, "
            "consecutive_failures, total_attempts, total_successes, total_nonempty, "
            "total_items, blocked_until, last_notified_at, last_success_at, "
            "last_nonempty_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (
                update.feed_id,
                update.job_kind,
                update.state,
                update.cause,
                update.attempted_at,
                int(update.success),
                int(update.nonempty),
                update.item_count,
                update.duration_ms,
                update.scheduler_lag_ms,
                update.attempted_at,
                0 if update.success else 1,
                int(update.success),
                int(update.nonempty),
                update.item_count,
                blocked_until,
                update.attempted_at if should_log else None,
                update.attempted_at if update.success else None,
                update.attempted_at if update.nonempty else None,
            ),
        )

    @staticmethod
    def _should_log_health(
        update: HealthUpdate,
        changed: bool,
        recovered: bool,
        previous_notified: int | None,
        reminder_seconds: int,
    ) -> bool:
        return (
            changed
            or recovered
            or (
                update.state in {"failed", "blocked"}
                and (
                    previous_notified is None
                    or update.attempted_at - int(previous_notified) >= reminder_seconds
                )
            )
        )

    def get_blocked_until(self, feed_id: str) -> int | None:
        row = self._connection.execute(
            "SELECT MAX(blocked_until) FROM health_state "
            "WHERE feed_id = ? AND blocked_until IS NOT NULL AND blocked_until > unixepoch()",
            (feed_id,),
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def record_job_timing(
        self,
        feed_id: str,
        job_kind: str,
        duration_ms: int,
        scheduler_lag_ms: int,
    ) -> None:
        """Update timing columns on an existing health row only."""
        with self._connection:
            self._connection.execute(
                "UPDATE health_state SET duration_ms = ?, scheduler_lag_ms = ? "
                "WHERE feed_id = ? AND job_kind = ?",
                (duration_ms, scheduler_lag_ms, feed_id, job_kind),
            )

    def list_health(self, feed_id: str | None = None) -> tuple[HealthRecord, ...]:
        where = FEED_WHERE_CLAUSE if feed_id is not None else ""
        query = (
            "SELECT feed_id, job_kind, state, cause, attempted_at, success, nonempty, "
            "item_count, duration_ms, scheduler_lag_ms, transition_at, "
            "consecutive_failures, total_attempts, total_successes, total_nonempty, "
            "total_items, blocked_until, last_notified_at, last_success_at, "
            "last_nonempty_at FROM health_state"
        )
        query += where
        query += " ORDER BY feed_id, job_kind"
        rows = self._connection.execute(
            query,
            () if feed_id is None else (feed_id,),
        )
        return tuple(
            HealthRecord(
                feed_id=row[0],
                job_kind=row[1],
                state=row[2],
                cause=row[3],
                attempted_at=row[4],
                success=bool(row[5]),
                nonempty=bool(row[6]),
                item_count=int(row[7]),
                duration_ms=row[8],
                scheduler_lag_ms=row[9],
                transition_at=row[10],
                consecutive_failures=int(row[11]),
                total_attempts=int(row[12]),
                total_successes=int(row[13]),
                total_nonempty=int(row[14]),
                total_items=int(row[15]),
                blocked_until=row[16],
                last_notified_at=row[17],
                last_success_at=row[18],
                last_nonempty_at=row[19],
            )
            for row in rows
        )
