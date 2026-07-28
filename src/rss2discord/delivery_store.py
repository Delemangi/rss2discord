import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Self

from rss2discord.price_amount import canonicalize_price_amount


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    feed_id: str
    product_id: str
    amount: Decimal
    formatted: str
    currency: str


class DeliveryStore:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        try:
            self._initialize()
        except sqlite3.Error:
            self._connection.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def has_delivered(self, feed_id: str, entry_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM delivered_entries WHERE feed_id = ? AND entry_id = ?",
            (feed_id, entry_id),
        ).fetchone()
        return row is not None

    def mark_delivered(self, feed_id: str, entry_id: str) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO delivered_entries (feed_id, entry_id) "
                "VALUES (?, ?)",
                (feed_id, entry_id),
            )

    def count_delivered(self, feed_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM delivered_entries WHERE feed_id = ?",
            (feed_id,),
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])

    def is_feed_initialized(self, feed_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM initialized_feeds WHERE feed_id = ?",
            (feed_id,),
        ).fetchone()
        return row is not None

    def seed_feed(self, feed_id: str, entry_ids: Iterable[str]) -> bool:
        """Atomically record entries when completing a feed's first sync."""
        with self._connection:
            initialized = self._connection.execute(
                "INSERT OR IGNORE INTO initialized_feeds (feed_id) VALUES (?)",
                (feed_id,),
            ).rowcount
            if initialized == 0:
                return False
            self._connection.executemany(
                "INSERT OR IGNORE INTO delivered_entries (feed_id, entry_id) "
                "VALUES (?, ?)",
                ((feed_id, entry_id) for entry_id in entry_ids),
            )
        return True

    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]:
        rows = self._connection.execute(
            "SELECT product_id, amount, formatted, currency "
            "FROM price_snapshots "
            "WHERE feed_id = ? "
            "ORDER BY product_id "
            "LIMIT ?",
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
            self._connection.executemany(
                "INSERT INTO price_snapshots "
                "(feed_id, product_id, amount, formatted, currency) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(feed_id, product_id) DO UPDATE SET "
                "amount = excluded.amount, "
                "formatted = excluded.formatted, "
                "currency = excluded.currency, "
                "updated_at = unixepoch() "
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

    def _initialize(self) -> None:
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS delivered_entries ("
                "feed_id TEXT NOT NULL, "
                "entry_id TEXT NOT NULL, "
                "delivered_at INTEGER NOT NULL DEFAULT (unixepoch()), "
                "PRIMARY KEY (feed_id, entry_id)"
                ") WITHOUT ROWID",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS initialized_feeds ("
                "feed_id TEXT NOT NULL PRIMARY KEY, "
                "initialized_at INTEGER NOT NULL DEFAULT (unixepoch())"
                ") WITHOUT ROWID",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS price_snapshots ("
                "feed_id TEXT NOT NULL, "
                "product_id TEXT NOT NULL, "
                "amount TEXT NOT NULL, "
                "formatted TEXT NOT NULL, "
                "currency TEXT NOT NULL, "
                "updated_at INTEGER NOT NULL DEFAULT (unixepoch()), "
                "PRIMARY KEY (feed_id, product_id)"
                ") WITHOUT ROWID",
            )
            legacy_price_snapshots_exist = self._connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'anhoch_price_snapshots'",
            ).fetchone()
            if legacy_price_snapshots_exist is not None:
                self._connection.execute(
                    "INSERT OR IGNORE INTO price_snapshots "
                    "(feed_id, product_id, amount, formatted, currency, updated_at) "
                    "SELECT feed_id, CAST(product_id AS TEXT), amount, formatted, currency, updated_at "
                    "FROM anhoch_price_snapshots",
                )
                self._connection.execute("DROP TABLE anhoch_price_snapshots")
            self._connection.execute(
                "INSERT OR IGNORE INTO initialized_feeds (feed_id) "
                "SELECT DISTINCT feed_id FROM delivered_entries",
            )
