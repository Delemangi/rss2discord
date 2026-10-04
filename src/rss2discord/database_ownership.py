"""Non-blocking lifetime ownership for local database writer processes.

The lock file is deliberately never unlinked: deleting it would permit two
processes to lock different inodes. SQLite connections remain thread-bound.
"""

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class DatabaseOwnershipError(RuntimeError):
    """Another writer owns the database (or ownership cannot be established)."""


class DatabaseOwnership:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self._file: BinaryIO | None = None

    def __enter__(self) -> Self:
        if self._file is not None:
            raise DatabaseOwnershipError("database ownership is already acquired")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.database_path.with_name(
            self.database_path.name + ".writer.lock",
        )
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise DatabaseOwnershipError(
                "database writer ownership unavailable; stop all writer services "
                "before running offline administration",
            ) from error
        self._file = handle
        return self

    def require(self, database_path: Path) -> None:
        if self._file is None or database_path.resolve() != self.database_path:
            raise DatabaseOwnershipError(
                "offline reconciliation requires database ownership",
            )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is not None:
            # Closing the descriptor releases the OS lock, including on failure.
            self._file.close()
            self._file = None
