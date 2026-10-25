"""Offline coverage and pinned-epoch comparison, never approval."""

from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any

from rss2discord.gjirafa_observation.store import inspect, readonly


def report(path: Path) -> dict[str, Any]:
    inspect(path)
    with closing(readonly(path)) as db:
        db.execute("BEGIN")
        known, mapped = db.execute("SELECT count(*),count(url) FROM items").fetchone()
        epoch = db.execute(
            "SELECT id,reference_completed_epoch,cursor,completed FROM epochs ORDER BY id DESC LIMIT 1",
        ).fetchone()
        coverage_per_feed = {
            feed: dict(
                zip(
                    (
                        "known",
                        "mapped",
                        "URL_unknown",
                        "validated",
                        "blocked",
                        "failed",
                        "cooldown",
                        "unknown",
                        "unchecked",
                    ),
                    counts,
                    strict=True,
                ),
            )
            for feed, *counts in db.execute(
                """
                SELECT i.feed_id, count(*), count(i.url), count(*)-count(i.url),
                    sum(CASE WHEN o.outcome='validated' THEN 1 ELSE 0 END),
                    sum(CASE WHEN o.outcome='blocked' THEN 1 ELSE 0 END),
                    sum(CASE WHEN o.outcome='failed' THEN 1 ELSE 0 END),
                    sum(CASE WHEN o.outcome='cooldown' THEN 1 ELSE 0 END),
                    sum(CASE WHEN o.outcome='unknown' THEN 1 ELSE 0 END),
                    count(*)-count(o.item)
                FROM items i LEFT JOIN observations o ON o.item=i.ordinal AND o.epoch=?
                GROUP BY i.feed_id ORDER BY i.feed_id
            """,
                (epoch[0] if epoch else None,),
            )
        }
        result: dict[str, Any] = {
            "manifest_fingerprint": db.execute(
                "SELECT value FROM metadata WHERE key='fingerprint'",
            ).fetchone()[0],
            "coverage_per_feed": coverage_per_feed,
            "known": known,
            "mapped": mapped,
            "URL_unknown": known - mapped,
            "validated": 0,
            "blocked": 0,
            "failed": 0,
            "cooldown": 0,
            "unknown": 0,
            "unchecked": known,
            "comparable": 0,
            "incomparable": 0,
            "changed_distinct_ids": 0,
            "changed_per_feed": {},
            "review_warning": False,
            "epoch": None,
            "state": "not started",
            "reference_completed_epoch": None,
            "notice": "Initial observations are not an approved baseline. No approval or delivery behavior exists.",
        }
        if epoch is None:
            return result
        current, reference, cursor, completed = epoch
        result.update(
            epoch=current,
            reference_completed_epoch=reference,
            state="manifest pass completed"
            if completed
            else "manifest pass in progress",
            unchecked=known - cursor,
        )
        result.update(
            db.execute(
                "SELECT outcome,count(*) FROM observations WHERE epoch=? GROUP BY outcome",
                (current,),
            ).fetchall(),
        )
        comparable = 0
        changed: set[tuple[str, str]] = set()
        feeds: dict[str, set[str]] = {}
        for feed, product, amount, currency, old_amount, old_currency in db.execute(
            """
            SELECT i.feed_id,i.product_id,c.amount,c.currency,r.amount,r.currency
            FROM observations c JOIN items i ON i.ordinal=c.item
            LEFT JOIN observations r ON r.item=c.item AND r.epoch=? AND r.outcome='validated'
            WHERE c.epoch=? AND c.outcome='validated'
        """,
            (reference, current),
        ):
            if old_amount is None or currency != old_currency:
                continue
            comparable += 1
            if Decimal(amount) != Decimal(old_amount):
                changed.add((feed, product))
                feeds.setdefault(feed, set()).add(product)
        result.update(
            comparable=comparable,
            incomparable=cursor - comparable,
            changed_distinct_ids=len(changed),
            changed_per_feed={k: len(v) for k, v in feeds.items()},
            review_warning=any(len(ids) > 100 for ids in feeds.values()),
        )
        return result
