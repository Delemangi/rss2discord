import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import BaseModel, ConfigDict, ValidationError

from configuration import FeedConfig

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2


class LegacyFeedState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    processed_ids: tuple[str, ...] = ()


class LegacyState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feeds: dict[str, LegacyFeedState]


@dataclass(frozen=True, slots=True)
class UnsupportedSchemaVersionError(Exception):
    version: int

    def __str__(self) -> str:
        return f"unsupported delivery database schema version: {self.version}"


class DeliveryStore:
    def __init__(
        self,
        database_path: Path,
        legacy_state_path: Path,
        feeds: Sequence[FeedConfig],
    ) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        try:
            self._initialize(legacy_state_path, feeds)
        except (
            json.JSONDecodeError,
            OSError,
            sqlite3.Error,
            UnicodeDecodeError,
            UnsupportedSchemaVersionError,
            ValidationError,
        ):
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

    def _initialize(
        self,
        legacy_state_path: Path,
        feeds: Sequence[FeedConfig],
    ) -> None:
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")

        version_row = self._connection.execute("PRAGMA user_version").fetchone()
        version = 0 if version_row is None else int(version_row[0])
        if version > SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(version=version)
        imported_count = 0
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if version < 1:
                self._connection.execute(
                    "CREATE TABLE delivered_entries ("
                    "feed_id TEXT NOT NULL, "
                    "entry_id TEXT NOT NULL, "
                    "delivered_at INTEGER NOT NULL DEFAULT (unixepoch()), "
                    "PRIMARY KEY (feed_id, entry_id)"
                    ") WITHOUT ROWID",
                )
            if version < 2:
                self._connection.execute(
                    "CREATE TABLE legacy_delivered_entries ("
                    "feed_url TEXT NOT NULL, "
                    "entry_id TEXT NOT NULL, "
                    "PRIMARY KEY (feed_url, entry_id)"
                    ") WITHOUT ROWID",
                )
                if version == 1:
                    self._stage_existing_deliveries(feeds)
                    try:
                        self._stage_legacy_state(legacy_state_path)
                    except (
                        json.JSONDecodeError,
                        OSError,
                        UnicodeDecodeError,
                        ValidationError,
                    ) as error:
                        logger.warning(
                            "Could not read legacy state during schema upgrade; "
                            "using existing delivery records (%s)",
                            type(error).__name__,
                        )
                else:
                    self._stage_legacy_state(legacy_state_path)
            imported_count = self._map_legacy_state(feeds)
            self._connection.execute("PRAGMA user_version = 2")

        if imported_count > 0:
            logger.info("Imported %d legacy delivery records", imported_count)

    def _stage_legacy_state(
        self,
        legacy_state_path: Path,
    ) -> None:
        if not legacy_state_path.exists():
            return

        with legacy_state_path.open(encoding="utf-8") as legacy_file:
            legacy_state = LegacyState.model_validate(json.load(legacy_file))
        for feed_url, feed_state in legacy_state.feeds.items():
            for entry_id in feed_state.processed_ids:
                self._connection.execute(
                    "INSERT OR IGNORE INTO legacy_delivered_entries "
                    "(feed_url, entry_id) VALUES (?, ?)",
                    (feed_url, entry_id),
                )

    def _stage_existing_deliveries(self, feeds: Sequence[FeedConfig]) -> None:
        for feed in feeds:
            self._connection.execute(
                "INSERT OR IGNORE INTO legacy_delivered_entries (feed_url, entry_id) "
                "SELECT ?, entry_id FROM delivered_entries WHERE feed_id = ?",
                (feed.url, feed.id),
            )

    def _map_legacy_state(self, feeds: Sequence[FeedConfig]) -> int:
        imported_count = 0
        for feed in feeds:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO delivered_entries (feed_id, entry_id) "
                "SELECT ?, entry_id FROM legacy_delivered_entries WHERE feed_url = ?",
                (feed.id, feed.url),
            )
            imported_count += cursor.rowcount
        return imported_count
