import json
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import BaseModel, ConfigDict, ValidationError

from configuration import FeedConfig

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


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
        if version == SCHEMA_VERSION:
            return

        imported_count = 0
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "CREATE TABLE delivered_entries ("
                "feed_id TEXT NOT NULL, "
                "entry_id TEXT NOT NULL, "
                "delivered_at INTEGER NOT NULL DEFAULT (unixepoch()), "
                "PRIMARY KEY (feed_id, entry_id)"
                ") WITHOUT ROWID",
            )
            imported_count = self._import_legacy_state(legacy_state_path, feeds)
            self._connection.execute("PRAGMA user_version = 1")

        if imported_count > 0:
            logger.info("Imported %d legacy delivery records", imported_count)

    def _import_legacy_state(
        self,
        legacy_state_path: Path,
        feeds: Sequence[FeedConfig],
    ) -> int:
        if not legacy_state_path.exists():
            return 0

        with legacy_state_path.open(encoding="utf-8") as legacy_file:
            legacy_state = LegacyState.model_validate(json.load(legacy_file))

        feed_ids_by_url: defaultdict[str, list[str]] = defaultdict(list)
        for feed in feeds:
            feed_ids_by_url[feed.url].append(feed.id)

        imported_count = 0
        for feed_url, feed_state in legacy_state.feeds.items():
            for feed_id in feed_ids_by_url[feed_url]:
                for entry_id in feed_state.processed_ids:
                    cursor = self._connection.execute(
                        "INSERT OR IGNORE INTO delivered_entries "
                        "(feed_id, entry_id) VALUES (?, ?)",
                        (feed_id, entry_id),
                    )
                    imported_count += cursor.rowcount

        return imported_count
