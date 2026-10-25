"""Private SQLite journal with fail-closed identity checks."""

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from rss2discord.database_ownership import DatabaseOwnership
from rss2discord.gjirafa_observation_models import ObservationItem

FILENAME = "gjirafa-observation.sqlite3"
APPLICATION_ID = 0x474A4F42
MARKER = "gjirafa-observation-v1"


def readonly(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    return db


def inspect(path: Path) -> None:
    with closing(readonly(path)) as db:
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("not an observation database")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("unsupported observation schema")
        if db.execute("SELECT value FROM metadata WHERE key='marker'").fetchone() != (
            MARKER,
        ):
            raise ValueError("missing observation schema marker")
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if tables != {"metadata", "items", "epochs", "observations"}:
            raise ValueError("foreign observation schema")
        if (
            db.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()
            is None
        ):
            raise ValueError("incomplete observation initialization")


def initialize(path: Path, items: list[ObservationItem], fingerprint: str) -> None:
    with DatabaseOwnership(path):
        if path.exists():
            inspect(path)
            with closing(readonly(path)) as db:
                existing = db.execute(
                    "SELECT value FROM metadata WHERE key='fingerprint'",
                ).fetchone()[0]
            if existing != fingerprint:
                raise ValueError("manifest replacement is forbidden")
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.close(descriptor)
            _build_store(temporary, items, fingerprint)
            inspect(temporary)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            # Atomic create, never replace: even an uncooperative writer's file
            # appearing after the initial existence check must remain untouched.
            os.link(temporary, path)
        finally:
            # Only this invocation's private paths; never remove the writer lock
            # or sweep orphaned files left by a different/terminated invocation.
            for suffix in ("-journal", "-wal", "-shm", ""):
                temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)


def _build_store(path: Path, items: list[ObservationItem], fingerprint: str) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
        db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE items(ordinal INTEGER PRIMARY KEY, feed_id TEXT NOT NULL,
                    host TEXT NOT NULL, product_id TEXT NOT NULL, url TEXT,
                    UNIQUE(feed_id,product_id));
                CREATE TABLE epochs(id INTEGER PRIMARY KEY, reference_completed_epoch INTEGER,
                    cursor INTEGER NOT NULL, completed INTEGER NOT NULL);
                CREATE TABLE observations(epoch INTEGER NOT NULL, item INTEGER NOT NULL,
                    outcome TEXT NOT NULL, observed_at INTEGER NOT NULL, amount TEXT,
                    currency TEXT, canonical_url TEXT, reason TEXT,
                    PRIMARY KEY(epoch,item));
            """)
        db.executemany(
            "INSERT INTO metadata VALUES (?,?)",
            [
                ("marker", MARKER),
                ("fingerprint", fingerprint),
            ],
        )
        db.executemany(
            "INSERT INTO items VALUES (?,?,?,?,?)",
            [(n, i.feed_id, i.host, i.product_id, i.url) for n, i in enumerate(items)],
        )


def open_writer(path: Path) -> sqlite3.Connection:
    inspect(path)
    return sqlite3.connect(path)
