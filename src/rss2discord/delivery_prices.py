"""Price-snapshot persistence and bounded price history."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from decimal import Decimal

from rss2discord.delivery_rows import upsert_price_snapshots
from rss2discord.models import PriceSnapshot


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
            "SELECT product_id, amount, formatted, currency, available "
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
                available=None if available is None else bool(available),
            )
            for product_id, amount, formatted, currency, available in rows
        )

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None:
        self.upsert_price_snapshots((snapshot,))

    def upsert_price_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> None:
        with self._connection:
            upsert_price_snapshots(self._connection, snapshots)
