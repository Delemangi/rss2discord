"""Price-snapshot persistence."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from decimal import Decimal

from rss2discord.models import PriceSnapshot
from rss2discord.price_amount import canonicalize_price_amount


def _upsert_price_snapshots(
    connection: sqlite3.Connection,
    snapshots: Iterable[PriceSnapshot],
) -> None:
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
        (
            (
                snapshot.feed_id,
                snapshot.product_id,
                canonicalize_price_amount(snapshot.amount),
                snapshot.formatted,
                snapshot.currency,
            )
            for snapshot in snapshots
        ),
    )


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
