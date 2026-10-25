"""Bounded fair passes and a read-only source health gate."""

import time
from contextlib import closing
from pathlib import Path

from rss2discord.database_ownership import DatabaseOwnership
from rss2discord.gjirafa_observation.store import open_writer, readonly
from rss2discord.gjirafa_observation_models import (
    ObservationFetcher,
    ObservationFetchError,
    ObservationItem,
)


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
        feed_hosts: dict[str, str] = {}
        for item in items:
            if item.feed_id in feed_hosts and feed_hosts[item.feed_id] != item.host:
                raise ValueError("mixed source feed hosts")
            feed_hosts[item.feed_id] = item.host
        for feed_id, host in feed_hosts.items():
            rows = db.execute(
                "SELECT blocked_until FROM health_state WHERE feed_id=? AND job_kind IN ('ordinary','price')",
                (feed_id,),
            ).fetchall()
            if not rows:
                raise ValueError("source health feed mapping unavailable")
            for (blocked,) in rows:
                if blocked is not None and (type(blocked) is not int or blocked < 0):
                    raise ValueError("invalid source cooldown")
                result[host] = max(result.get(host, 0), blocked or 0)
        return result


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
        row = db.execute(
            "SELECT id,reference_completed_epoch,cursor,completed FROM epochs ORDER BY id DESC LIMIT 1",
        ).fetchone()
        if row is None or row[3]:
            reference = row[0] if row else None
            epoch = (row[0] + 1) if row else 1
            with db:
                db.execute("INSERT INTO epochs VALUES (?,?,0,0)", (epoch, reference))
            cursor = 0
        else:
            epoch, _, cursor, _ = row
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
            outcome, reason = "unknown", None
            amount = currency = canonical_url = None
            observed_at = now
            fatal = False
            if blocked > now:
                outcome, reason = "cooldown", "host cooldown active"
            elif item.url is None:
                reason = "URL unknown"
            else:
                try:
                    scalar = fetcher.fetch(item)
                    if (
                        not scalar.amount.is_finite()
                        or scalar.amount <= 0
                        or not scalar.currency
                    ):
                        raise ObservationFetchError("invalid scalar")  # noqa: TRY301
                    amount, currency = str(scalar.amount), scalar.currency
                    canonical_url, observed_at = (
                        scalar.canonical_url,
                        scalar.observed_at,
                    )
                    outcome = "validated"
                except ObservationFetchError as error:
                    reason, fatal = error.reason, error.fatal
                    outcome = (
                        "blocked"
                        if error.stop_host
                        or error.blocked_until
                        or error.reason == "ScalarContractRejected"
                        else "failed"
                    )
                    if error.stop_host or error.blocked_until:
                        blocked = max(blocked, error.blocked_until or now + 6 * 3600)
            with db:
                if blocked > now:
                    db.execute(
                        "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                        ("cooldown:" + item.host, str(blocked)),
                    )
                db.execute(
                    "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
                    (
                        epoch,
                        ordinal,
                        outcome,
                        observed_at,
                        amount,
                        currency,
                        canonical_url,
                        reason,
                    ),
                )
                db.execute(
                    "UPDATE epochs SET cursor=?,completed=? WHERE id=?",
                    (
                        ordinal + 1,
                        int(ordinal + 1 == len(items)),
                        epoch,
                    ),
                )
            attempted += 1
            if fatal:
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
