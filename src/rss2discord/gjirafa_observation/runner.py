"""Bounded fair passes and a read-only source health gate."""

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from rss2discord.database_ownership import DatabaseOwnership
from rss2discord.gjirafa_observation.store import open_writer, readonly
from rss2discord.gjirafa_observation_models import (
    ObservationFetcher,
    ObservationFetchError,
    ObservationItem,
    ScalarObservation,
)


def _feed_hosts(items: list[ObservationItem]) -> dict[str, str]:
    hosts: dict[str, str] = {}
    for item in items:
        if item.feed_id in hosts and hosts[item.feed_id] != item.host:
            raise ValueError("mixed source feed hosts")
        hosts[item.feed_id] = item.host
    return hosts


def _feed_cooldown(db: sqlite3.Connection, feed_id: str) -> int:
    rows = db.execute(
        "SELECT blocked_until FROM health_state WHERE feed_id=? AND job_kind IN ('ordinary','price')",
        (feed_id,),
    ).fetchall()
    if not rows:
        raise ValueError("source health feed mapping unavailable")
    cooldown = 0
    for (blocked,) in rows:
        if blocked is not None and (type(blocked) is not int or blocked < 0):
            raise ValueError("invalid source cooldown")
        cooldown = max(cooldown, blocked or 0)
    return cooldown


def source_gate(
    source: Path,
    observation: Path,
    items: list[ObservationItem],
) -> dict[str, int]:
    if source.resolve() == observation.resolve() or (
        source.exists() and observation.exists() and source.samefile(observation)
    ):
        raise ValueError("source state must not be the observation database")
    with closing(readonly(source)) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(health_state)")}
        if not {"feed_id", "job_kind", "blocked_until"} <= columns:
            raise ValueError("source health schema unavailable")
        result: dict[str, int] = {}
        for feed_id, host in _feed_hosts(items).items():
            result[host] = max(result.get(host, 0), _feed_cooldown(db, feed_id))
        return result


def _select_epoch(db: sqlite3.Connection) -> tuple[int, int]:
    row = db.execute(
        "SELECT id,reference_completed_epoch,cursor,completed FROM epochs ORDER BY id DESC LIMIT 1",
    ).fetchone()
    if row is not None and not row[3]:
        return row[0], row[2]
    reference = row[0] if row else None
    epoch = (row[0] + 1) if row else 1
    with db:
        db.execute("INSERT INTO epochs VALUES (?,?,0,0)", (epoch, reference))
    return epoch, 0


@dataclass(frozen=True)
class _Outcome:
    kind: str
    observed_at: int
    blocked_until: int
    scalar: ScalarObservation | None = None
    reason: str | None = None
    fatal: bool = False


def _fetch_outcome(
    fetcher: ObservationFetcher,
    item: ObservationItem,
    now: int,
    blocked: int,
) -> _Outcome:
    if blocked > now:
        return _Outcome("cooldown", now, blocked, reason="host cooldown active")
    if item.url is None:
        return _Outcome("unknown", now, blocked, reason="URL unknown")
    try:
        scalar = fetcher.fetch(item)
        _validate_scalar(scalar)
        return _Outcome("validated", scalar.observed_at, blocked, scalar=scalar)
    except ObservationFetchError as error:
        kind = (
            "blocked"
            if (
                error.stop_host
                or error.blocked_until
                or error.reason == "ScalarContractRejected"
            )
            else "failed"
        )
        if error.stop_host or error.blocked_until:
            blocked = max(blocked, error.blocked_until or now + 6 * 3600)
        return _Outcome(kind, now, blocked, reason=error.reason, fatal=error.fatal)


def _validate_scalar(scalar: ScalarObservation) -> None:
    if not scalar.amount.is_finite() or scalar.amount <= 0 or not scalar.currency:
        raise ObservationFetchError("invalid scalar")


def _persist_outcome(
    db: sqlite3.Connection,
    *,
    epoch: int,
    ordinal: int,
    total: int,
    host: str,
    outcome: _Outcome,
    now: int,
) -> None:
    scalar = outcome.scalar
    with db:
        if outcome.blocked_until > now:
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                ("cooldown:" + host, str(outcome.blocked_until)),
            )
        db.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
            (
                epoch,
                ordinal,
                outcome.kind,
                outcome.observed_at,
                str(scalar.amount) if scalar else None,
                scalar.currency if scalar else None,
                scalar.canonical_url if scalar else None,
                outcome.reason,
            ),
        )
        db.execute(
            "UPDATE epochs SET cursor=?,completed=? WHERE id=?",
            (
                ordinal + 1,
                int(ordinal + 1 == total),
                epoch,
            ),
        )


def run_owned(
    path: Path,
    source: Path,
    fetcher: ObservationFetcher,
    *,
    ownership: DatabaseOwnership,
    limit: int = 25,
) -> int:
    """Caller must retain database ownership throughout client construction and run."""
    ownership.require(path)
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    with closing(open_writer(path)) as db:
        items = [
            ObservationItem(*row)
            for row in db.execute(
                "SELECT feed_id,host,product_id,url FROM items ORDER BY ordinal",
            )
        ]
        source_gate(source, path, items)
        epoch, cursor = _select_epoch(db)
        attempted = 0
        for ordinal in range(cursor, min(cursor + limit, len(items))):
            item = items[ordinal]
            gates = source_gate(source, path, items)
            now = int(time.time())
            local = db.execute(
                "SELECT value FROM metadata WHERE key=?",
                ("cooldown:" + item.host,),
            ).fetchone()
            blocked = max(gates.get(item.host, 0), int(local[0]) if local else 0)
            outcome = _fetch_outcome(fetcher, item, now, blocked)
            _persist_outcome(
                db,
                epoch=epoch,
                ordinal=ordinal,
                total=len(items),
                host=item.host,
                outcome=outcome,
                now=now,
            )
            attempted += 1
            if outcome.fatal:
                break
        return attempted


def run(
    path: Path,
    source: Path,
    fetcher: ObservationFetcher,
    *,
    limit: int = 25,
) -> int:
    with DatabaseOwnership(path) as ownership:
        return run_owned(path, source, fetcher, ownership=ownership, limit=limit)
