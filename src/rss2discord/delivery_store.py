"""SQLite persistence for delivery state, recovery manifests, and health."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from decimal import Decimal
from itertools import chain
from pathlib import Path
from types import TracebackType
from typing import Self

from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.price_safety import (
    HEALTH_REMINDER_SECONDS,
    MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN,
    MAX_PRICE_MANIFEST_ITEMS,
    canonical_manifest_fingerprint,
)
from rss2discord.recovery_models import (
    BaselineCandidateSummary,
    HealthNotice,
    HealthRecord,
    HealthUpdate,
    PriceBatch,
    PriceBatchItem,
    PriceBatchSummary,
    PriceChangeRecord,
    PriceDeliveryClaim,
    PriceSnapshot,
)


class DeliveryStore:
    """Own the additive state tables while preserving legacy delivery rows."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.execute("PRAGMA foreign_keys = ON")
        try:
            self._initialize()
        except sqlite3.Error:
            self._connection.close()
            raise

    @property
    def database_path(self) -> Path:
        return self._database_path

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

    # Legacy ordinary delivery state -------------------------------------

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
        return 0 if row is None else int(row[0])

    def is_feed_initialized(self, feed_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM initialized_feeds WHERE feed_id = ?",
            (feed_id,),
        ).fetchone()
        return row is not None

    def seed_feed(self, feed_id: str, entry_ids: Iterable[str]) -> bool:
        """Atomically record legacy entries on the first sync only."""
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

    # Price snapshots -----------------------------------------------------

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
            self._connection.executemany(
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

    # Additive price-change manifests ------------------------------------

    def record_price_change_candidate(
        self,
        *,
        feed_id: str,
        provider: str,
        fingerprint: str,
        catalog_count: int,
        available_count: int,
        items: Iterable[PriceChangeRecord],
    ) -> PriceBatch:
        """Replace the one candidate manifest, preserving an exact retry."""
        records_list: list[PriceChangeRecord] = []
        for item in items:
            records_list.append(item)
            if len(records_list) > MAX_PRICE_MANIFEST_ITEMS:
                raise ValueError("price manifest item limit exceeded")
        records = tuple(sorted(records_list, key=lambda item: item.product_id))
        _validate_price_records(records, feed_id)
        if not records:
            raise ValueError("price manifest must contain items")
        if not 0 <= available_count <= catalog_count:
            raise ValueError("catalog counts are inconsistent")
        if len(records) > available_count:
            raise ValueError("price manifest exceeds available catalog count")
        expected_fingerprint = canonical_manifest_fingerprint(
            feed_id=feed_id,
            provider=provider,
            items=records,
        )
        if fingerprint != expected_fingerprint:
            raise ValueError("price manifest fingerprint does not match its items")
        with self._connection:
            existing = self._connection.execute(
                "SELECT batch_id FROM price_change_batches "
                "WHERE feed_id = ? AND status = 'candidate'",
                (feed_id,),
            ).fetchone()
            if existing is not None:
                old = self._connection.execute(
                    "SELECT fingerprint FROM price_change_batches WHERE batch_id = ?",
                    (existing[0],),
                ).fetchone()
                if old is not None and old[0] == fingerprint:
                    self._connection.execute(
                        "UPDATE price_change_batches SET catalog_count = ?, "
                        "available_count = ?, updated_at = unixepoch() "
                        "WHERE batch_id = ?",
                        (catalog_count, available_count, existing[0]),
                    )
                    return self._load_price_batch(int(existing[0]))
                self._connection.execute(
                    "DELETE FROM price_change_batches WHERE batch_id = ?",
                    (existing[0],),
                )
            cursor = self._connection.execute(
                "INSERT INTO price_change_batches "
                "(feed_id, provider, fingerprint, catalog_count, available_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (feed_id, provider, fingerprint, catalog_count, available_count),
            )
            if cursor.lastrowid is None:  # pragma: no cover - SQLite always assigns one
                raise RuntimeError("SQLite did not assign a price batch ID")
            batch_id = int(cursor.lastrowid)
            self._insert_price_items(batch_id, records)
            self._prune_terminal_price_batches(feed_id)
        return self._load_price_batch(batch_id)

    def load_active_price_batch(self, feed_id: str) -> PriceBatch | None:
        row = self._connection.execute(
            "SELECT batch_id FROM price_change_batches "
            "WHERE feed_id = ? AND status IN ('approved', 'paused') "
            "ORDER BY batch_id DESC LIMIT 1",
            (feed_id,),
        ).fetchone()
        return None if row is None else self._load_price_batch(int(row[0]))

    def load_price_batch(self, batch_id: int) -> PriceBatch | None:
        row = self._connection.execute(
            "SELECT 1 FROM price_change_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        return None if row is None else self._load_price_batch(batch_id)

    def approve_price_change_batch(
        self,
        *,
        feed_id: str,
        fingerprint: str,
        reason: str,
    ) -> PriceBatch:
        """Approve only an exact candidate/paused fingerprint and reason."""
        _require_reason(reason)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            active = self._connection.execute(
                "SELECT batch_id FROM price_change_batches "
                "WHERE feed_id = ? AND status IN ('approved', 'paused') "
                "AND fingerprint <> ? LIMIT 1",
                (feed_id, fingerprint),
            ).fetchone()
            if active is not None:
                raise ValueError("another price batch is already active")
            row = self._connection.execute(
                "SELECT batch_id, status FROM price_change_batches "
                "WHERE feed_id = ? AND fingerprint = ? "
                "AND status IN ('candidate', 'paused')",
                (feed_id, fingerprint),
            ).fetchone()
            if row is None:
                raise ValueError("price batch fingerprint is not pending approval")
            batch_id = int(row[0])
            self._connection.execute(
                "UPDATE price_change_batches SET status = 'approved', reason = ?, "
                "approved_at = unixepoch(), paused_at = NULL WHERE batch_id = ?",
                (reason, batch_id),
            )
        return self._load_price_batch(batch_id)

    def revoke_price_change_batch(
        self,
        batch_id: int | None = None,
        fingerprint: str | None = None,
        reason: str = "",
        *,
        feed_id: str | None = None,
    ) -> PriceBatch:
        """Revoke a candidate or active batch after exact audit selection."""
        _require_reason(reason)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._find_price_batch(batch_id, feed_id, fingerprint)
            if row is None:
                raise ValueError("price batch not found")
            selected_id, selected_fingerprint, status = row
            if fingerprint is not None and selected_fingerprint != fingerprint:
                raise ValueError("price batch fingerprint mismatch")
            if status in {"completed", "revoked"}:
                raise ValueError("terminal price batch cannot be revoked")
            self._connection.execute(
                "UPDATE price_change_batches SET status = 'revoked', reason = ?, "
                "updated_at = unixepoch() WHERE batch_id = ?",
                (reason, selected_id),
            )
            selected_batch = self._load_price_batch(int(selected_id))
            selected_feed_id = feed_id
            if selected_feed_id is None:
                feed_row = self._connection.execute(
                    "SELECT feed_id FROM price_change_batches WHERE batch_id = ?",
                    (selected_id,),
                ).fetchone()
                selected_feed_id = None if feed_row is None else str(feed_row[0])
            self._prune_terminal_price_batches(selected_feed_id)
        return selected_batch

    def pause_price_change_batch(self, batch_id: int, reason: str) -> PriceBatch:
        """Persist a fail-closed pause without changing its manifest."""
        _require_reason(reason)
        with self._connection:
            row = self._connection.execute(
                "SELECT status FROM price_change_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if row is None or row[0] not in {"approved", "paused"}:
                raise ValueError("only an active price batch can be paused")
            self._connection.execute(
                "UPDATE price_change_batches SET status = 'paused', reason = ?, "
                "paused_at = unixepoch(), updated_at = unixepoch() WHERE batch_id = ?",
                (reason, batch_id),
            )
        return self._load_price_batch(batch_id)

    def claim_price_delivery_attempt(
        self,
        batch_id: int,
        product_id: str,
    ) -> PriceDeliveryClaim | None:
        """Atomically claim exactly one pending approved item.

        The write is intentionally separate from network delivery.  A pending
        open claim may be reclaimed after a process restart; doing so advances
        its generation and invalidates any late callback for the old claim.
        """
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                active = self._connection.execute(
                    "SELECT 1 FROM price_change_batches "
                    "WHERE batch_id = ? AND status = 'approved'",
                    (batch_id,),
                ).fetchone()
                if active is None:
                    self._connection.rollback()
                    return None
                updated = self._connection.execute(
                    "UPDATE price_change_batch_items SET attempt_count = attempt_count + 1, "
                    "last_attempt_at = unixepoch(), claim_generation = claim_generation + 1, "
                    "claim_open = 1 WHERE batch_id = ? AND product_id = ? "
                    "AND status = 'pending'",
                    (batch_id, product_id),
                )
                if updated.rowcount != 1:
                    self._connection.rollback()
                    return None
                generation_row = self._connection.execute(
                    "SELECT claim_generation FROM price_change_batch_items "
                    "WHERE batch_id = ? AND product_id = ?",
                    (batch_id, product_id),
                ).fetchone()
                if generation_row is None:  # pragma: no cover - conditional update
                    self._connection.rollback()
                    return None
                self._connection.commit()
                return PriceDeliveryClaim(batch_id, product_id, int(generation_row[0]))
            except BaseException:
                self._connection.rollback()
                raise

    def release_price_delivery_attempt(self, claim: PriceDeliveryClaim) -> bool:
        """Close a matching claim without changing delivery state."""
        with self._connection:
            updated = self._connection.execute(
                "UPDATE price_change_batch_items SET claim_open = 0 "
                "WHERE batch_id = ? AND product_id = ? AND claim_generation = ? "
                "AND claim_open = 1 AND status = 'pending'",
                (claim.batch_id, claim.product_id, claim.generation),
            )
            changed = updated.rowcount == 1
            if changed:
                self._prune_terminal_price_batches_for_batch(claim.batch_id)
            return changed

    def record_approved_price_delivery(
        self,
        claim: PriceDeliveryClaim,
        snapshot: PriceSnapshot,
    ) -> None:
        """Atomically acknowledge a matching generation-scoped claim."""
        if snapshot.product_id != claim.product_id:
            raise ValueError("delivered snapshot identity mismatch")
        with self._connection:
            batch = self._connection.execute(
                "SELECT feed_id, status FROM price_change_batches WHERE batch_id = ?",
                (claim.batch_id,),
            ).fetchone()
            item = self._connection.execute(
                "SELECT current_amount, current_formatted, current_currency, status, "
                "attempt_count, claim_generation, claim_open "
                "FROM price_change_batch_items WHERE batch_id = ? AND product_id = ?",
                (claim.batch_id, claim.product_id),
            ).fetchone()
            if batch is None or batch[1] not in {"approved", "revoked"} or item is None:
                raise ValueError("price delivery is not approved or revoked")
            if item[3] != "pending":
                raise ValueError("price delivery item is already completed")
            if int(item[4]) <= 0 or int(item[5]) != claim.generation or not item[6]:
                raise ValueError("price delivery attempt was not reserved")
            if snapshot.feed_id != batch[0] or snapshot.product_id != claim.product_id:
                raise ValueError("delivered snapshot identity mismatch")
            if (
                canonicalize_price_amount(snapshot.amount) != item[0]
                or snapshot.formatted != item[1]
                or snapshot.currency != item[2]
            ):
                raise ValueError("delivered snapshot does not match manifest target")
            updated = self._connection.execute(
                "UPDATE price_change_batch_items SET status = 'delivered', "
                "delivered_at = unixepoch(), claim_open = 0 WHERE batch_id = ? "
                "AND product_id = ? AND status = 'pending' AND attempt_count > 0 "
                "AND claim_generation = ? AND claim_open = 1",
                (
                    claim.batch_id,
                    claim.product_id,
                    claim.generation,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("price delivery item is no longer pending")
            self._connection.execute(
                "INSERT INTO price_snapshots "
                "(feed_id, product_id, amount, formatted, currency) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(feed_id, product_id) DO UPDATE SET amount = excluded.amount, "
                "formatted = excluded.formatted, currency = excluded.currency, "
                "updated_at = unixepoch()",
                (
                    snapshot.feed_id,
                    snapshot.product_id,
                    canonicalize_price_amount(snapshot.amount),
                    snapshot.formatted,
                    snapshot.currency,
                ),
            )
            self._prune_terminal_price_batches_for_batch(claim.batch_id)

    def complete_price_change_batch_if_drained(self, batch_id: int) -> bool:
        """Complete an approved batch only after every item is delivered."""
        with self._connection:
            row = self._connection.execute(
                "SELECT status FROM price_change_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            pending = self._connection.execute(
                "SELECT COUNT(*) FROM price_change_batch_items "
                "WHERE batch_id = ? AND status <> 'delivered'",
                (batch_id,),
            ).fetchone()
            if (
                row is None
                or row[0] != "approved"
                or pending is None
                or pending[0] != 0
            ):
                return False
            self._connection.execute(
                "UPDATE price_change_batches SET status = 'completed', "
                "completed_at = unixepoch(), updated_at = unixepoch() WHERE batch_id = ?",
                (batch_id,),
            )
            feed_row = self._connection.execute(
                "SELECT feed_id FROM price_change_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            self._prune_terminal_price_batches(
                None if feed_row is None else str(feed_row[0]),
            )
        return True

    def list_price_change_batches(
        self,
        feed_id: str | None = None,
        *,
        include_terminal: bool = False,
    ) -> tuple[PriceBatchSummary, ...]:
        where = ""
        parameters: tuple[object, ...] = ()
        if feed_id is not None:
            where = " WHERE feed_id = ?"
            parameters = (feed_id,)
        if not include_terminal:
            where += " AND " if where else " WHERE "
            where += "status NOT IN ('completed', 'revoked')"
        query = "SELECT batch_id FROM price_change_batches" + where  # noqa: S608
        query += " ORDER BY batch_id DESC"
        rows = self._connection.execute(query, parameters)
        return tuple(self._load_price_summary(int(row[0])) for row in rows)

    def select_normal_price_deliveries(
        self,
        *,
        feed_id: str,
        product_ids: Iterable[str],
        limit: int = MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN,
    ) -> tuple[str, ...] | None:
        """Serialize normal-price rotation and return the next fair IDs.

        This is a short database-only transaction.  Confirmation and sending
        must happen after it commits.  ``None`` means that an approved or
        paused recovery batch owns the feed; an empty tuple is a valid
        zero-change scan and therefore permits ordinary silent updates.
        """
        if not 1 <= limit <= MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN:
            raise ValueError("limit must be between 1 and 10")
        normalized = tuple(sorted(set(product_ids)))
        if any(not product_id for product_id in normalized):
            raise ValueError("normal price product IDs must be non-empty")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                active = self._connection.execute(
                    "SELECT 1 FROM price_change_batches "
                    "WHERE feed_id = ? AND status IN ('approved', 'paused') LIMIT 1",
                    (feed_id,),
                ).fetchone()
                if active is not None:
                    self._connection.commit()
                    return None

                candidate = self._connection.execute(
                    "SELECT batch_id FROM price_change_batches "
                    "WHERE feed_id = ? AND status = 'candidate' LIMIT 1",
                    (feed_id,),
                ).fetchone()
                if candidate is not None:
                    self._connection.execute(
                        "UPDATE price_change_batches SET status = 'revoked', "
                        "reason = 'ChangeSetNoLongerRequiresApproval', "
                        "updated_at = unixepoch() WHERE batch_id = ? AND status = 'candidate'",
                        (candidate[0],),
                    )
                    self._prune_terminal_price_batches(feed_id)

                if not normalized:
                    self._connection.commit()
                    return ()

                cursor = self._connection.execute(
                    "SELECT last_product_id FROM price_normal_delivery_cursors "
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
                    "INSERT INTO price_normal_delivery_cursors "
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

    # Baseline candidates -------------------------------------------------

    def record_feed_baseline_candidate(
        self,
        *,
        feed_id: str,
        entry_ids: Iterable[str],
        reason: str,
    ) -> BaselineCandidateSummary:
        """Persist one complete inventory candidate without touching deliveries."""
        _require_reason(reason)
        raw_ids = tuple(entry_ids)
        if len(raw_ids) != len(set(raw_ids)):
            raise ValueError("baseline entry IDs must be unique")
        ids = tuple(sorted(raw_ids))
        if any(not entry_id for entry_id in ids):
            raise ValueError("baseline entry IDs must be non-empty")
        fingerprint = _baseline_fingerprint(feed_id, ids)
        with self._connection:
            initialized_baseline = self._connection.execute(
                "SELECT fingerprint FROM baseline_states WHERE feed_id = ?",
                (feed_id,),
            ).fetchone()
            if (
                initialized_baseline is not None
                and initialized_baseline[0] != fingerprint
            ):
                raise ValueError("complete baseline cannot be replaced")
            existing = self._connection.execute(
                "SELECT fingerprint FROM baseline_candidates WHERE feed_id = ?",
                (feed_id,),
            ).fetchone()
            if existing is not None and existing[0] == fingerprint:
                return self.load_baseline_candidate(feed_id)  # type: ignore[return-value]
            self._connection.execute(
                "DELETE FROM baseline_candidates WHERE feed_id = ?",
                (feed_id,),
            )
            self._connection.execute(
                "INSERT INTO baseline_candidates (feed_id, fingerprint, reason) "
                "VALUES (?, ?, ?)",
                (feed_id, fingerprint, reason),
            )
            self._connection.executemany(
                "INSERT INTO baseline_candidate_entries (feed_id, entry_id) VALUES (?, ?)",
                ((feed_id, entry_id) for entry_id in ids),
            )
        return self.load_baseline_candidate(feed_id)  # type: ignore[return-value]

    def load_baseline_candidate(self, feed_id: str) -> BaselineCandidateSummary | None:
        row = self._connection.execute(
            "SELECT fingerprint, status, reason, created_at, approved_at "
            "FROM baseline_candidates WHERE feed_id = ?",
            (feed_id,),
        ).fetchone()
        if row is None:
            return None
        ids = tuple(
            entry[0]
            for entry in self._connection.execute(
                "SELECT entry_id FROM baseline_candidate_entries "
                "WHERE feed_id = ? ORDER BY entry_id",
                (feed_id,),
            )
        )
        return BaselineCandidateSummary(
            feed_id,
            row[0],
            ids,
            row[1],
            row[2],
            row[3],
            row[4],
        )

    def approve_feed_baseline_candidate(
        self,
        *,
        feed_id: str,
        fingerprint: str,
        reason: str,
    ) -> BaselineCandidateSummary:
        _require_reason(reason)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT fingerprint, status FROM baseline_candidates WHERE feed_id = ?",
                (feed_id,),
            ).fetchone()
            if row is None or row[0] != fingerprint:
                raise ValueError("baseline fingerprint mismatch")
            if row[1] == "approved":
                candidate = self.load_baseline_candidate(feed_id)
                if candidate is None:  # pragma: no cover - transaction guarantees this
                    raise RuntimeError("baseline candidate disappeared")
                return candidate
            self._connection.execute(
                "INSERT OR IGNORE INTO baseline_states "
                "(feed_id, fingerprint, complete, approved_at) VALUES (?, ?, 1, unixepoch()) "
                "ON CONFLICT(feed_id) DO UPDATE SET fingerprint = excluded.fingerprint, "
                "complete = 1, approved_at = unixepoch()",
                (feed_id, fingerprint),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO baselined_entries (feed_id, entry_id) "
                "SELECT feed_id, entry_id FROM baseline_candidate_entries WHERE feed_id = ?",
                (feed_id,),
            )
            approved = self._connection.execute(
                "UPDATE baseline_candidates SET status = 'approved', reason = ?, "
                "approved_at = unixepoch() WHERE feed_id = ? AND fingerprint = ? "
                "AND status = 'candidate'",
                (reason, feed_id, fingerprint),
            )
            if approved.rowcount != 1:
                raise ValueError("baseline candidate is no longer pending")
            self._connection.execute(
                "INSERT OR IGNORE INTO initialized_feeds (feed_id) VALUES (?)",
                (feed_id,),
            )
        candidate = self.load_baseline_candidate(feed_id)
        if candidate is None:  # pragma: no cover - transaction guarantees this
            raise RuntimeError("baseline candidate disappeared")
        return candidate

    def has_baselined(self, feed_id: str, entry_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM baselined_entries WHERE feed_id = ? AND entry_id = ?",
            (feed_id, entry_id),
        ).fetchone()
        return row is not None

    def has_complete_baseline(self, feed_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM baseline_states WHERE feed_id = ? AND complete = 1",
            (feed_id,),
        ).fetchone()
        return row is not None

    def has_handled_entry(self, feed_id: str, entry_id: str) -> bool:
        return self.has_delivered(feed_id, entry_id) or self.has_baselined(
            feed_id,
            entry_id,
        )

    def initialize_feed_with_baseline(
        self,
        *,
        feed_id: str,
        entry_ids: Iterable[str],
        reason: str,
    ) -> BaselineCandidateSummary:
        """Atomically create a fresh feed's explicit, complete baseline."""
        _require_reason(reason)
        if self.is_feed_initialized(feed_id):
            raise ValueError(
                "initialized legacy feed requires explicit baseline approval",
            )
        raw_ids = tuple(entry_ids)
        if len(raw_ids) != len(set(raw_ids)):
            raise ValueError("baseline entry IDs must be unique")
        ids = tuple(sorted(raw_ids))
        if any(not entry_id for entry_id in ids):
            raise ValueError("baseline entry IDs must be non-empty")
        fingerprint = _baseline_fingerprint(feed_id, ids)
        with self._connection:
            self._connection.execute(
                "INSERT INTO baseline_candidates (feed_id, fingerprint, status, reason) "
                "VALUES (?, ?, 'approved', ?)",
                (feed_id, fingerprint, reason),
            )
            self._connection.executemany(
                "INSERT INTO baseline_candidate_entries (feed_id, entry_id) VALUES (?, ?)",
                ((feed_id, entry_id) for entry_id in ids),
            )
            self._connection.execute(
                "INSERT INTO baseline_states (feed_id, fingerprint, complete, approved_at) "
                "VALUES (?, ?, 1, unixepoch())",
                (feed_id, fingerprint),
            )
            self._connection.executemany(
                "INSERT INTO baselined_entries (feed_id, entry_id) VALUES (?, ?)",
                ((feed_id, entry_id) for entry_id in ids),
            )
            self._connection.execute(
                "INSERT INTO initialized_feeds (feed_id) VALUES (?)",
                (feed_id,),
            )
        candidate = self.load_baseline_candidate(feed_id)
        if candidate is None:  # pragma: no cover
            raise RuntimeError("baseline was not persisted")
        return candidate

    def list_baseline_candidates(
        self,
        feed_id: str | None = None,
    ) -> tuple[BaselineCandidateSummary, ...]:
        where = " WHERE feed_id = ?" if feed_id is not None else ""
        query = "SELECT feed_id FROM baseline_candidates" + where  # noqa: S608
        query += " ORDER BY feed_id"
        rows = self._connection.execute(
            query,
            () if feed_id is None else (feed_id,),
        )
        return tuple(
            candidate
            for row in rows
            if (candidate := self.load_baseline_candidate(str(row[0]))) is not None
        )

    # Health and scheduler timing ----------------------------------------

    def record_health(
        self,
        update: HealthUpdate,
        *,
        reminder_seconds: int = HEALTH_REMINDER_SECONDS,
    ) -> HealthNotice:
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
                "quarantined",
                "recovery_required",
            } and update.state not in {
                "failed",
                "blocked",
                "quarantined",
                "recovery_required",
            }
            should_log = (
                changed
                or recovered
                or (
                    update.state
                    in {"failed", "blocked", "quarantined", "recovery_required"}
                    and (
                        previous_notified is None
                        or update.attempted_at - int(previous_notified)
                        >= reminder_seconds
                    )
                )
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
        """Update timing columns without changing domain health state."""
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO health_state "
                "(feed_id, job_kind, state, duration_ms, scheduler_lag_ms) "
                "VALUES (?, ?, 'unknown', ?, ?)",
                (feed_id, job_kind, duration_ms, scheduler_lag_ms),
            )
            self._connection.execute(
                "UPDATE health_state SET duration_ms = ?, scheduler_lag_ms = ? "
                "WHERE feed_id = ? AND job_kind = ?",
                (duration_ms, scheduler_lag_ms, feed_id, job_kind),
            )

    def list_health(self, feed_id: str | None = None) -> tuple[HealthRecord, ...]:
        where = " WHERE feed_id = ?" if feed_id is not None else ""
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

    # SQLite schema and conversion helpers --------------------------------

    def _initialize(self) -> None:
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS delivered_entries (feed_id TEXT NOT NULL, "
                "entry_id TEXT NOT NULL, delivered_at INTEGER NOT NULL DEFAULT (unixepoch()), "
                "PRIMARY KEY (feed_id, entry_id)) WITHOUT ROWID",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS initialized_feeds (feed_id TEXT NOT NULL PRIMARY KEY, "
                "initialized_at INTEGER NOT NULL DEFAULT (unixepoch())) WITHOUT ROWID",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS price_snapshots (feed_id TEXT NOT NULL, "
                "product_id TEXT NOT NULL, amount TEXT NOT NULL, formatted TEXT NOT NULL, "
                "currency TEXT NOT NULL, updated_at INTEGER NOT NULL DEFAULT (unixepoch()), "
                "PRIMARY KEY (feed_id, product_id)) WITHOUT ROWID",
            )
            legacy = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'anhoch_price_snapshots'",
            ).fetchone()
            if legacy is not None:
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
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS price_change_batches ("
                "batch_id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id TEXT NOT NULL, "
                "provider TEXT NOT NULL, fingerprint TEXT NOT NULL, catalog_count INTEGER NOT NULL, "
                "available_count INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'candidate' "
                "CHECK(status IN ('candidate','approved','paused','completed','revoked')), "
                "reason TEXT, created_at INTEGER NOT NULL DEFAULT (unixepoch()), "
                "approved_at INTEGER, paused_at INTEGER, completed_at INTEGER, "
                "updated_at INTEGER NOT NULL DEFAULT (unixepoch()))",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS price_change_batch_items ("
                "batch_id INTEGER NOT NULL REFERENCES price_change_batches(batch_id) ON DELETE CASCADE, "
                "product_id TEXT NOT NULL, previous_amount TEXT NOT NULL, previous_formatted TEXT NOT NULL, "
                "previous_currency TEXT NOT NULL, current_amount TEXT NOT NULL, current_formatted TEXT NOT NULL, "
                "current_currency TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' "
                "CHECK(status IN ('pending','delivered')), attempt_count INTEGER NOT NULL DEFAULT 0, "
                "last_attempt_at INTEGER, ordinal INTEGER NOT NULL, delivered_at INTEGER, "
                "PRIMARY KEY(batch_id, product_id)) WITHOUT ROWID",
            )
            item_columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(price_change_batch_items)",
                )
            }
            if "claim_generation" not in item_columns:
                self._connection.execute(
                    "ALTER TABLE price_change_batch_items ADD COLUMN "
                    "claim_generation INTEGER NOT NULL DEFAULT 0",
                )
            if "claim_open" not in item_columns:
                self._connection.execute(
                    "ALTER TABLE price_change_batch_items ADD COLUMN "
                    "claim_open INTEGER NOT NULL DEFAULT 0",
                )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS price_normal_delivery_cursors ("
                "feed_id TEXT PRIMARY KEY, last_product_id TEXT NOT NULL, "
                "updated_at INTEGER NOT NULL DEFAULT (unixepoch())) WITHOUT ROWID",
            )
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS price_change_candidate_one "
                "ON price_change_batches(feed_id) WHERE status = 'candidate'",
            )
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS price_change_active_one "
                "ON price_change_batches(feed_id) WHERE status IN ('approved','paused')",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS baseline_candidates (feed_id TEXT PRIMARY KEY, "
                "fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate' "
                "CHECK(status IN ('candidate','approved')), reason TEXT NOT NULL, "
                "created_at INTEGER NOT NULL DEFAULT (unixepoch()), approved_at INTEGER) WITHOUT ROWID",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS baseline_candidate_entries (feed_id TEXT NOT NULL "
                "REFERENCES baseline_candidates(feed_id) ON DELETE CASCADE, entry_id TEXT NOT NULL, "
                "PRIMARY KEY(feed_id, entry_id)) WITHOUT ROWID",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS baseline_states (feed_id TEXT PRIMARY KEY, "
                "fingerprint TEXT NOT NULL, complete INTEGER NOT NULL, approved_at INTEGER NOT NULL) WITHOUT ROWID",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS baselined_entries (feed_id TEXT NOT NULL "
                "REFERENCES baseline_states(feed_id) ON DELETE CASCADE, entry_id TEXT NOT NULL, "
                "PRIMARY KEY(feed_id, entry_id)) WITHOUT ROWID",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS health_state (feed_id TEXT NOT NULL, job_kind TEXT NOT NULL, "
                "state TEXT NOT NULL, cause TEXT, attempted_at INTEGER, success INTEGER NOT NULL DEFAULT 0, "
                "nonempty INTEGER NOT NULL DEFAULT 0, item_count INTEGER NOT NULL DEFAULT 0, "
                "duration_ms INTEGER, scheduler_lag_ms INTEGER, transition_at INTEGER, "
                "consecutive_failures INTEGER NOT NULL DEFAULT 0, total_attempts INTEGER NOT NULL DEFAULT 0, "
                "total_successes INTEGER NOT NULL DEFAULT 0, total_nonempty INTEGER NOT NULL DEFAULT 0, "
                "total_items INTEGER NOT NULL DEFAULT 0, blocked_until INTEGER, last_notified_at INTEGER, "
                "last_success_at INTEGER, last_nonempty_at INTEGER, "
                "PRIMARY KEY(feed_id, job_kind)) WITHOUT ROWID",
            )
            health_columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(health_state)")
            }
            for column in ("last_success_at", "last_nonempty_at"):
                if column not in health_columns:
                    self._connection.execute(
                        f"ALTER TABLE health_state ADD COLUMN {column} INTEGER",
                    )

    def _insert_price_items(
        self,
        batch_id: int,
        records: tuple[PriceChangeRecord, ...],
    ) -> None:
        self._connection.executemany(
            "INSERT INTO price_change_batch_items "
            "(batch_id, product_id, previous_amount, previous_formatted, previous_currency, "
            "current_amount, current_formatted, current_currency, ordinal) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    batch_id,
                    record.product_id,
                    canonicalize_price_amount(record.previous.amount),
                    record.previous.formatted,
                    record.previous.currency,
                    canonicalize_price_amount(record.current.amount),
                    record.current.formatted,
                    record.current.currency,
                    ordinal,
                )
                for ordinal, record in enumerate(records)
            ),
        )

    def _load_price_batch(self, batch_id: int) -> PriceBatch:
        rows = self._connection.execute(
            "SELECT b.batch_id, b.feed_id, b.provider, b.fingerprint, b.status, "
            "b.catalog_count, b.available_count, b.created_at, b.reason, b.approved_at, "
            "b.paused_at, b.completed_at, i.product_id, i.previous_amount, "
            "i.previous_formatted, i.previous_currency, i.current_amount, "
            "i.current_formatted, i.current_currency, i.status, i.attempt_count, "
            "i.last_attempt_at, i.ordinal, i.delivered_at "
            "FROM price_change_batches b LEFT JOIN price_change_batch_items i "
            "ON i.batch_id = b.batch_id WHERE b.batch_id = ? ORDER BY i.ordinal",
            (batch_id,),
        )
        first = rows.fetchone()
        if first is None:
            raise ValueError("price batch not found")
        items: list[PriceBatchItem] = []
        for row in chain((first,), rows):
            if row[12] is None:
                continue
            product_id = str(row[12])
            items.append(
                PriceBatchItem(
                    product_id=product_id,
                    previous=PriceSnapshot(
                        row[1],
                        product_id,
                        Decimal(row[13]),
                        row[14],
                        row[15],
                    ),
                    current=PriceSnapshot(
                        row[1],
                        product_id,
                        Decimal(row[16]),
                        row[17],
                        row[18],
                    ),
                    status=row[19],
                    attempt_count=int(row[20]),
                    last_attempt_at=row[21],
                    ordinal=int(row[22]),
                    delivered_at=row[23],
                ),
            )
        summary = PriceBatchSummary(
            batch_id=int(first[0]),
            feed_id=first[1],
            provider=first[2],
            fingerprint=first[3],
            status=first[4],
            catalog_count=int(first[5]),
            available_count=int(first[6]),
            item_count=len(items),
            pending_count=sum(item.status == "pending" for item in items),
            delivered_count=sum(item.status == "delivered" for item in items),
            created_at=int(first[7]),
            reason=first[8],
            approved_at=first[9],
            paused_at=first[10],
            completed_at=first[11],
        )
        return PriceBatch(summary=summary, items=tuple(items))

    def _load_price_summary(self, batch_id: int) -> PriceBatchSummary:
        row = self._connection.execute(
            "SELECT b.batch_id, b.feed_id, b.provider, b.fingerprint, b.status, "
            "b.catalog_count, b.available_count, b.created_at, b.reason, b.approved_at, "
            "b.paused_at, b.completed_at, COUNT(i.product_id), "
            "COALESCE(SUM(i.status = 'pending'), 0), COALESCE(SUM(i.status = 'delivered'), 0) "
            "FROM price_change_batches b LEFT JOIN price_change_batch_items i ON i.batch_id = b.batch_id "
            "WHERE b.batch_id = ? GROUP BY b.batch_id",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise ValueError("price batch not found")
        return PriceBatchSummary(
            batch_id=int(row[0]),
            feed_id=row[1],
            provider=row[2],
            fingerprint=row[3],
            status=row[4],
            catalog_count=int(row[5]),
            available_count=int(row[6]),
            created_at=int(row[7]),
            reason=row[8],
            approved_at=row[9],
            paused_at=row[10],
            completed_at=row[11],
            item_count=int(row[12]),
            pending_count=int(row[13]),
            delivered_count=int(row[14]),
        )

    def _prune_terminal_price_batches_for_batch(self, batch_id: int) -> None:
        row = self._connection.execute(
            "SELECT feed_id FROM price_change_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if row is not None:
            self._prune_terminal_price_batches(str(row[0]))

    def _find_price_batch(
        self,
        batch_id: int | None,
        feed_id: str | None,
        fingerprint: str | None,
    ) -> tuple[int, str, str] | None:
        if batch_id is not None:
            if feed_id is not None:
                return self._connection.execute(
                    "SELECT batch_id, fingerprint, status FROM price_change_batches "
                    "WHERE batch_id = ? AND feed_id = ?",
                    (batch_id, feed_id),
                ).fetchone()
            return self._connection.execute(
                "SELECT batch_id, fingerprint, status FROM price_change_batches "
                "WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
        if feed_id is None or fingerprint is None:
            return None
        rows = self._connection.execute(
            "SELECT batch_id, fingerprint, status FROM price_change_batches "
            "WHERE feed_id = ? AND fingerprint = ? "
            "AND status IN ('candidate', 'approved', 'paused') "
            "ORDER BY batch_id DESC LIMIT 2",
            (feed_id, fingerprint),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("price batch fingerprint is ambiguous")
        return None if not rows else rows[0]

    def _prune_terminal_price_batches(self, feed_id: str | None) -> None:
        if feed_id is None:
            return
        self._connection.execute(
            "DELETE FROM price_change_batches WHERE batch_id IN ("
            "SELECT batch_id FROM price_change_batches WHERE feed_id = ? "
            "AND status IN ('completed','revoked') "
            "AND NOT EXISTS (SELECT 1 FROM price_change_batch_items i "
            "WHERE i.batch_id = price_change_batches.batch_id AND i.claim_open = 1) "
            "AND batch_id NOT IN (SELECT batch_id FROM price_change_batches "
            "WHERE feed_id = ? AND status IN ('completed','revoked') "
            "ORDER BY batch_id DESC LIMIT 10))",
            (feed_id, feed_id),
        )


def _validate_price_records(
    records: tuple[PriceChangeRecord, ...],
    feed_id: str,
) -> None:
    product_ids = [record.product_id for record in records]
    if any(not product_id for product_id in product_ids):
        raise ValueError("price product IDs must be non-empty")
    if len(product_ids) != len(set(product_ids)):
        raise ValueError("price manifest product IDs must be unique")
    for record in records:
        if record.previous.feed_id != feed_id or record.current.feed_id != feed_id:
            raise ValueError("price manifest feed identity mismatch")
        if (
            record.previous.product_id != record.product_id
            or record.current.product_id != record.product_id
        ):
            raise ValueError("price manifest product identity mismatch")


def _require_reason(reason: str) -> None:
    if not reason.strip():
        raise ValueError("an explicit non-empty reason is required")


def _baseline_fingerprint(feed_id: str, entry_ids: tuple[str, ...]) -> str:
    payload = {"version": 1, "feed": feed_id, "entry_ids": entry_ids}
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["DeliveryStore", "PriceDeliveryClaim", "PriceSnapshot"]
