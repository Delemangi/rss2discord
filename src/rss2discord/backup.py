"""Consistent SQLite backup for Watchtower pre-update lifecycle hooks."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

DEFAULT_STATE_DB_PATH: Final = "/app/data/state.db"
DEFAULT_BACKUP_RETENTION: Final = 10
BACKUP_PREFIX: Final = "state-pre-update-"
TEMP_PREFIX: Final = ".state-pre-update-"
EX_TEMPFAIL: Final = 75
MAX_ERROR_CHARACTERS: Final = 300


def main() -> int:
    """Create and verify a pre-update backup, or fail closed with EX_TEMPFAIL."""
    temp_path: Path | None = None
    try:
        source_path = Path(os.environ.get("STATE_DB_PATH", DEFAULT_STATE_DB_PATH))
        backup_dir = Path(
            os.environ.get("BACKUP_DIR", str(source_path.parent / "backups")),
        )
        retention = _retention()
        if not source_path.is_file():
            raise FileNotFoundError(  # noqa: TRY301
                f"state database not found: {source_path}",
            )
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = _timestamp()
        final_path = backup_dir / f"{BACKUP_PREFIX}{timestamp}.db"
        temp_path = backup_dir / f"{TEMP_PREFIX}{timestamp}.db.tmp"
        _copy_and_verify(source_path, temp_path)
        temp_path.replace(final_path)
        temp_path = None
        _prune_backups(backup_dir, final_path, retention)
    except Exception as error:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        print(_sanitized_error(error), file=sys.stderr)
        return EX_TEMPFAIL
    print(final_path)
    return 0


def _retention() -> int:
    raw = os.environ.get("BACKUP_RETENTION", str(DEFAULT_BACKUP_RETENTION))
    try:
        retention = int(raw)
    except ValueError:
        raise ValueError("BACKUP_RETENTION must be a positive integer") from None
    if retention < 1:
        raise ValueError("BACKUP_RETENTION must be a positive integer")
    return retention


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def _copy_and_verify(source_path: Path, temp_path: Path) -> None:
    source = sqlite3.connect(
        source_path.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=10,
    )
    destination = sqlite3.connect(temp_path, timeout=10)
    try:
        _backup_database(source, destination)
        if not _integrity_ok(destination):
            raise RuntimeError("backup integrity check failed")
    finally:
        destination.close()
        source.close()


def _backup_database(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
) -> None:
    source.backup(destination)


def _integrity_ok(connection: sqlite3.Connection) -> bool:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    return rows == [("ok",)]


def _prune_backups(backup_dir: Path, keep: Path, retention: int) -> None:
    backups = sorted(
        (path for path in backup_dir.glob(f"{BACKUP_PREFIX}*.db") if path != keep),
        key=lambda path: path.name,
        reverse=True,
    )
    for old in backups[retention - 1 :]:
        try:
            old.unlink()
        except FileNotFoundError:
            continue


def _sanitized_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    if len(message) > MAX_ERROR_CHARACTERS:
        message = message[: MAX_ERROR_CHARACTERS - 1] + "…"
    detail = f": {message}" if message else ""
    return f"{type(error).__name__}{detail}"


if __name__ == "__main__":
    raise SystemExit(main())
