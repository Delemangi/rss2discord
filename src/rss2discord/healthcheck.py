"""Container healthcheck for the local SQLite state database."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Final

DEFAULT_STATE_DB_PATH: Final = "/app/data/state.db"
MAX_ERROR_CHARACTERS: Final = 300


def main() -> int:
    """Return 0 when the state database is readable and passes quick_check."""
    connection: sqlite3.Connection | None = None
    try:
        database_path = Path(os.environ.get("STATE_DB_PATH", DEFAULT_STATE_DB_PATH))
        if not database_path.is_file():
            raise FileNotFoundError(  # noqa: TRY301
                f"state database not found: {database_path}",
            )
        connection = sqlite3.connect(
            database_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
        )
        rows = connection.execute("PRAGMA quick_check").fetchall()
        if rows != [("ok",)]:
            raise RuntimeError("database quick_check failed")  # noqa: TRY301
    except Exception as error:
        print(_sanitized_error(error), file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()
    return 0


def _sanitized_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    if len(message) > MAX_ERROR_CHARACTERS:
        message = message[: MAX_ERROR_CHARACTERS - 1] + "…"
    detail = f": {message}" if message else ""
    return f"{type(error).__name__}{detail}"


if __name__ == "__main__":
    raise SystemExit(main())
