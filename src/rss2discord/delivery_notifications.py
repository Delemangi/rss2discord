"""Durable price-notification outbox and confirmation rotation."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from decimal import Decimal
from typing import Final

from rss2discord.delivery_rows import upsert_price_snapshots
from rss2discord.models import PriceSnapshot
from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.price_notifications import (
    PriceNotification,
    PriceNotificationRecord,
    entry_from_json,
    entry_to_json,
)

BEGIN_IMMEDIATE: Final = "BEGIN IMMEDIATE"
MAX_PRICE_NOTIFICATION_ERROR_CHARACTERS: Final = 200
MAX_PRICE_NOTIFICATION_ATTEMPTS: Final = 6


def sanitize_price_notification_error(error: str | None) -> str | None:
    """Keep only one bounded printable line of a delivery error."""
    if error is None:
        return None
    cleaned = "".join(
        character if character.isprintable() else " " for character in error
    )
    return " ".join(cleaned.split())[:MAX_PRICE_NOTIFICATION_ERROR_CHARACTERS]


def _validate_price_scan(
    feed_id: str,
    group_id: str,
    baselines: Sequence[PriceSnapshot],
    pending: Sequence[PriceNotification],
) -> None:
    if not group_id:
        raise ValueError("notification group ID must be non-empty")
    for snapshot in baselines:
        if snapshot.feed_id != feed_id:
            raise ValueError("baseline feed mismatch")
    for notification in pending:
        if notification.current.feed_id != feed_id:
            raise ValueError("notification feed mismatch")
        if notification.previous is not None and (
            notification.previous.feed_id != feed_id
        ):
            raise ValueError("notification previous feed mismatch")
        if notification.product_id != notification.current.product_id:
            raise ValueError("notification product mismatch")


def _price_notification_rows(
    feed_id: str,
    group_id: str,
    observed_at: int,
    pending: Sequence[PriceNotification],
) -> Iterator[tuple[object, ...]]:
    for notification in pending:
        yield (
            feed_id,
            group_id,
            notification.product_id,
            notification.entry.title,
            notification.entry.link,
            (
                None
                if notification.previous is None
                else notification.previous.formatted
            ),
            notification.current.formatted,
            (
                None
                if notification.previous is None
                else canonicalize_price_amount(notification.previous.amount)
            ),
            canonicalize_price_amount(notification.current.amount),
            notification.current.currency,
            entry_to_json(notification.entry),
            notification.source_title,
            observed_at,
        )


class PriceNotificationRepository:
    """Persist and read the durable price-notification outbox."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def record_price_scan(
        self,
        *,
        feed_id: str,
        group_id: str,
        observed_at: int,
        baseline_updates: Iterable[PriceSnapshot],
        notifications: Iterable[PriceNotification],
    ) -> int:
        """Atomically advance baselines and persist one scan's notifications."""
        baselines = tuple(baseline_updates)
        pending = tuple(notifications)
        _validate_price_scan(feed_id, group_id, baselines, pending)
        self._write_price_scan(feed_id, group_id, observed_at, baselines, pending)
        return len(pending)

    def _write_price_scan(
        self,
        feed_id: str,
        group_id: str,
        observed_at: int,
        baselines: Sequence[PriceSnapshot],
        pending: Sequence[PriceNotification],
    ) -> None:
        with self._connection:
            self._connection.execute(BEGIN_IMMEDIATE)
            try:
                upsert_price_snapshots(self._connection, baselines)
                self._connection.executemany(
                    "INSERT INTO price_notifications "
                    "(feed_id, group_id, product_id, title, link, previous_price, "
                    "current_price, previous_amount, current_amount, currency, "
                    "entry_json, source_title, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _price_notification_rows(feed_id, group_id, observed_at, pending),
                )
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def load_pending_price_group(
        self,
        feed_id: str,
    ) -> tuple[str, tuple[PriceNotificationRecord, ...]] | None:
        """Load the oldest still-pending group for one feed."""
        row = self._connection.execute(
            "SELECT group_id FROM price_notifications "
            "WHERE feed_id = ? AND status = 'pending' "
            "AND dead_lettered_at IS NULL "
            "ORDER BY notification_id LIMIT 1",
            (feed_id,),
        ).fetchone()
        if row is None:
            return None
        group_id = str(row[0])
        records = tuple(
            self._price_notification_record(item)
            for item in self._connection.execute(
                "SELECT notification_id, feed_id, group_id, product_id, title, link, "
                "previous_price, current_price, previous_amount, current_amount, "
                "currency, entry_json, source_title, "
                "observed_at, attempts FROM price_notifications "
                "WHERE feed_id = ? AND group_id = ? AND status = 'pending' "
                "AND dead_lettered_at IS NULL "
                "ORDER BY notification_id",
                (feed_id, group_id),
            )
        )
        return group_id, records

    @staticmethod
    def _price_notification_record(
        item: Sequence[object],
    ) -> PriceNotificationRecord:
        previous_amount = None if item[8] is None else Decimal(str(item[8]))
        if item[9] is not None:
            current_amount = Decimal(str(item[9]))
        elif previous_amount is not None:
            current_amount = previous_amount
        else:
            current_amount = Decimal(0)
        return PriceNotificationRecord(
            notification_id=int(str(item[0])),
            feed_id=str(item[1]),
            group_id=str(item[2]),
            product_id=str(item[3]),
            title=str(item[4]),
            link=str(item[5]),
            previous_price=None if item[6] is None else str(item[6]),
            current_price=str(item[7]),
            previous_amount=previous_amount,
            current_amount=current_amount,
            currency=str(item[10]),
            entry=entry_from_json(str(item[11])),
            source_title=str(item[12]),
            observed_at=int(str(item[13])),
            attempts=int(str(item[14])),
        )

    def mark_price_notifications_sent(
        self,
        notification_ids: Iterable[int],
    ) -> None:
        ids = tuple(notification_ids)
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        with self._connection:
            self._connection.execute(
                "UPDATE price_notifications SET status = 'sent', "  # noqa: S608
                "sent_at = unixepoch(), entry_json = '{}' "
                "WHERE status = 'pending' AND dead_lettered_at IS NULL "
                f"AND notification_id IN ({placeholders})",
                ids,
            )

    def record_price_notification_attempt(
        self,
        notification_ids: Iterable[int],
        error: str | None,
    ) -> None:
        ids = tuple(notification_ids)
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        with self._connection:
            self._connection.execute(
                "UPDATE price_notifications "  # noqa: S608
                "SET attempts = attempts + 1, last_attempt_at = unixepoch(), "
                f"last_error = ? WHERE notification_id IN ({placeholders})",
                (sanitize_price_notification_error(error), *ids),
            )
            self._connection.execute(
                "UPDATE price_notifications "  # noqa: S608
                "SET dead_lettered_at = unixepoch(), entry_json = '{}' "
                "WHERE status = 'pending' AND dead_lettered_at IS NULL "
                "AND attempts >= ? "
                f"AND notification_id IN ({placeholders})",
                (MAX_PRICE_NOTIFICATION_ATTEMPTS, *ids),
            )

    def count_pending_price_notifications(self, feed_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM price_notifications "
            "WHERE feed_id = ? AND status = 'pending' "
            "AND dead_lettered_at IS NULL",
            (feed_id,),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def count_pending_price_notifications_total(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM price_notifications "
            "WHERE status = 'pending' AND dead_lettered_at IS NULL",
        ).fetchone()
        return 0 if row is None else int(row[0])

    def count_dead_lettered_price_notifications(self, feed_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM price_notifications "
            "WHERE feed_id = ? AND status = 'pending' "
            "AND dead_lettered_at IS NOT NULL",
            (feed_id,),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def count_dead_lettered_price_notifications_total(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM price_notifications "
            "WHERE status = 'pending' AND dead_lettered_at IS NOT NULL",
        ).fetchone()
        return 0 if row is None else int(row[0])

    def list_price_notifications(
        self,
        feed_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        """Return outbox rows for read-only inspection."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if status not in {None, "pending", "sent", "dead_lettered"}:
            raise ValueError("status must be pending, sent, dead_lettered, or omitted")
        where: list[str] = []
        parameters: list[object] = []
        if feed_id is not None:
            where.append("feed_id = ?")
            parameters.append(feed_id)
        if status == "pending":
            where.append("status = 'pending' AND dead_lettered_at IS NULL")
        elif status == "sent":
            where.append("status = 'sent'")
        elif status == "dead_lettered":
            where.append("status = 'pending' AND dead_lettered_at IS NOT NULL")
        query = (
            "SELECT notification_id, feed_id, group_id, title, previous_price, "
            "current_price, currency, status, attempts, observed_at, sent_at, "
            "last_error, dead_lettered_at FROM price_notifications"
        )
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY feed_id, notification_id LIMIT ?"
        parameters.append(limit)
        return tuple(
            {
                "id": int(row[0]),
                "feed_id": row[1],
                "group_id": row[2],
                "title": row[3],
                "previous_price": row[4],
                "current_price": row[5],
                "currency": row[6],
                "status": "dead_lettered" if row[12] is not None else row[7],
                "attempts": int(row[8]),
                "observed_at": row[9],
                "sent_at": row[10],
                "last_error": row[11],
                "dead_lettered_at": row[12],
            }
            for row in self._connection.execute(query, parameters)
        )

    def select_price_confirmation_subset(
        self,
        *,
        feed_id: str,
        product_ids: Iterable[str],
        limit: int,
    ) -> tuple[str, ...]:
        """Rotate a confirmation subset using the durable cursor."""
        if limit < 1:
            raise ValueError("limit must be positive")
        normalized = tuple(sorted(set(product_ids)))
        if any(not product_id for product_id in normalized):
            raise ValueError("price confirmation product IDs must be non-empty")
        with self._connection:
            self._connection.execute(BEGIN_IMMEDIATE)
            try:
                if not normalized:
                    self._connection.commit()
                    return ()
                cursor = self._connection.execute(
                    "SELECT last_product_id FROM price_confirmation_cursors "
                    "WHERE feed_id = ?",
                    (feed_id,),
                ).fetchone()
                last_id = None if cursor is None else str(cursor[0])
                after = (
                    tuple(
                        product_id for product_id in normalized if product_id > last_id
                    )
                    if last_id is not None
                    else normalized
                )
                before = (
                    tuple(
                        product_id for product_id in normalized if product_id <= last_id
                    )
                    if last_id is not None
                    else ()
                )
                selected = (after + before)[:limit] if after else normalized[:limit]
                self._connection.execute(
                    "INSERT INTO price_confirmation_cursors "
                    "(feed_id, last_product_id, updated_at) VALUES (?, ?, unixepoch()) "
                    "ON CONFLICT(feed_id) DO UPDATE SET last_product_id = excluded.last_product_id, "
                    "updated_at = excluded.updated_at",
                    (feed_id, selected[-1]),
                )
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
                return selected
