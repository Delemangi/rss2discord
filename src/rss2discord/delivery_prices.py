"""Price-snapshot persistence and bounded price history."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from decimal import Decimal

from rss2discord.models import PriceSnapshot
from rss2discord.price_amount import canonicalize_price_amount

_SNAPSHOT_LOOKUP_CHUNK = 500

_SNAPSHOT_ROW = tuple[str, str, str, str, str]


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
    for feed_id, product_id, _amount, _formatted, _currency in snapshots:
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
        for feed_id, product_id, amount, formatted, currency in snapshots
        if existing.get((feed_id, product_id)) != (amount, currency)
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO price_history "
        "(feed_id, product_id, amount, formatted, currency, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def _upsert_price_snapshots(
    connection: sqlite3.Connection,
    snapshots: Iterable[PriceSnapshot],
) -> None:
    materialized = tuple(
        (
            snapshot.feed_id,
            snapshot.product_id,
            canonicalize_price_amount(snapshot.amount),
            snapshot.formatted,
            snapshot.currency,
        )
        for snapshot in snapshots
    )
    # History compares against the state before this batch, so the existing
    # rows are read before the upsert runs. The caller owns the transaction.
    existing = _existing_prices_by_feed(connection, materialized)
    connection.executemany(
        "INSERT INTO price_snapshots "
        "(feed_id, product_id, amount, formatted, currency) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(feed_id, product_id) DO UPDATE SET "
        "amount = excluded.amount, formatted = excluded.formatted, "
        "currency = excluded.currency, updated_at = unixepoch() "
        "WHERE price_snapshots.amount <> excluded.amount "
        "OR price_snapshots.formatted <> excluded.formatted "
        "OR price_snapshots.currency <> excluded.currency",
        materialized,
    )
    _append_price_history(connection, materialized, existing)


class PriceSnapshotRepository:
    """Persist source-neutral price snapshots for one feed."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]:
        rows = self._connection.execute(
            "SELECT product_id, amount, formatted, currency "
            "FROM price_snapshots WHERE feed_id = ? ORDER BY product_id LIMIT ?",
            (feed_id, -1 if limit is None else limit),
        )
        return tuple(
            PriceSnapshot(
                feed_id=feed_id,
                product_id=product_id,
                amount=Decimal(amount),
                formatted=formatted,
                currency=currency,
            )
            for product_id, amount, formatted, currency in rows
        )

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None:
        self.upsert_price_snapshots((snapshot,))

    def upsert_price_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> None:
        with self._connection:
            _upsert_price_snapshots(self._connection, snapshots)
