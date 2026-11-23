"""Atomic price-snapshot and history row writers shared by repositories."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable

from rss2discord.models import PriceSnapshot
from rss2discord.price_amount import canonicalize_price_amount

_SNAPSHOT_LOOKUP_CHUNK = 500

_SNAPSHOT_ROW = tuple[str, str, str, str, str, int | None]


def _existing_snapshot_prices(
    connection: sqlite3.Connection,
    feed_id: str,
    product_ids: Iterable[str],
) -> dict[str, tuple[str, str]]:
    """Read the persisted amount and currency for the requested products."""
    existing: dict[str, tuple[str, str]] = {}
    pending = tuple(product_ids)
    for start in range(0, len(pending), _SNAPSHOT_LOOKUP_CHUNK):
        chunk = pending[start : start + _SNAPSHOT_LOOKUP_CHUNK]
        placeholders = ", ".join("?" for _ in chunk)
        rows = connection.execute(
            "SELECT product_id, amount, currency FROM price_snapshots "  # noqa: S608
            f"WHERE feed_id = ? AND product_id IN ({placeholders})",
            (feed_id, *chunk),
        )
        existing.update(
            (str(product_id), (str(amount), str(currency)))
            for product_id, amount, currency in rows
        )
    return existing


def _existing_prices_by_feed(
    connection: sqlite3.Connection,
    snapshots: Iterable[_SNAPSHOT_ROW],
) -> dict[tuple[str, str], tuple[str, str]]:
    """Look up persisted prices per feed with a bounded IN clause."""
    by_feed: dict[str, list[str]] = {}
    for feed_id, product_id, _amount, _formatted, _currency, _available in snapshots:
        by_feed.setdefault(feed_id, []).append(product_id)
    existing: dict[tuple[str, str], tuple[str, str]] = {}
    for feed_id, product_ids in by_feed.items():
        existing.update(
            ((feed_id, product_id), value)
            for product_id, value in _existing_snapshot_prices(
                connection,
                feed_id,
                product_ids,
            ).items()
        )
    return existing


def _append_price_history(
    connection: sqlite3.Connection,
    snapshots: Iterable[_SNAPSHOT_ROW],
    existing: dict[tuple[str, str], tuple[str, str]],
) -> None:
    """Record one history row per new or amount/currency-changing snapshot."""
    observed_at = int(time.time())
    rows = [
        (feed_id, product_id, amount, formatted, currency, observed_at)
        for feed_id, product_id, amount, formatted, currency, _available in snapshots
        if existing.get((feed_id, product_id)) != (amount, currency)
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO price_history "
        "(feed_id, product_id, amount, formatted, currency, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def touch_price_snapshots(
    connection: sqlite3.Connection,
    feed_id: str,
    product_ids: Iterable[str],
    *,
    seen_at: int | None = None,
) -> None:
    """Mark observed products as seen inside the caller's transaction."""
    ids = tuple(product_ids)
    if not ids:
        return
    observed_at = int(time.time()) if seen_at is None else seen_at
    for start in range(0, len(ids), _SNAPSHOT_LOOKUP_CHUNK):
        chunk = ids[start : start + _SNAPSHOT_LOOKUP_CHUNK]
        placeholders = ", ".join("?" for _ in chunk)
        connection.execute(
            "UPDATE price_snapshots SET last_seen_at = ? "  # noqa: S608
            f"WHERE feed_id = ? AND product_id IN ({placeholders})",
            (observed_at, feed_id, *chunk),
        )


def upsert_price_snapshots(
    connection: sqlite3.Connection,
    snapshots: Iterable[PriceSnapshot],
) -> None:
    """Upsert snapshots and append history inside the caller's transaction."""
    materialized = tuple(
        (
            snapshot.feed_id,
            snapshot.product_id,
            canonicalize_price_amount(snapshot.amount),
            snapshot.formatted,
            snapshot.currency,
            None if snapshot.available is None else int(snapshot.available),
        )
        for snapshot in snapshots
    )
    # History compares against the state before this batch, so the existing
    # rows are read before the upsert runs. The caller owns the transaction.
    existing = _existing_prices_by_feed(connection, materialized)
    connection.executemany(
        "INSERT INTO price_snapshots "
        "(feed_id, product_id, amount, formatted, currency, available) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(feed_id, product_id) DO UPDATE SET "
        "amount = excluded.amount, formatted = excluded.formatted, "
        "currency = excluded.currency, available = excluded.available, "
        "updated_at = unixepoch() "
        "WHERE price_snapshots.amount <> excluded.amount "
        "OR price_snapshots.formatted <> excluded.formatted "
        "OR price_snapshots.currency <> excluded.currency "
        "OR price_snapshots.available IS NOT excluded.available",
        materialized,
    )
    _append_price_history(connection, materialized, existing)
