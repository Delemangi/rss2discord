import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rss2discord.admin import main
from rss2discord.database_ownership import DatabaseOwnership, DatabaseOwnershipError
from rss2discord.delivery_store import DeliveryStore


def test_two_process_writer_conflict_and_release(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    code = """import sys
from pathlib import Path
from rss2discord.database_ownership import DatabaseOwnership, DatabaseOwnershipError
try:
    with DatabaseOwnership(Path(sys.argv[1])):
        print('owned')
except DatabaseOwnershipError:
    print('conflict')
    sys.exit(3)
"""
    with DatabaseOwnership(database):
        result = subprocess.run(  # noqa: S603 - fixed interpreter and fixed test script
            [sys.executable, "-c", code, str(database)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 3
        assert result.stdout.strip() == "conflict"
    result = subprocess.run(  # noqa: S603 - fixed interpreter and fixed test script
        [sys.executable, "-c", code, str(database)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "owned"


def test_readers_work_while_application_owns_database_admin_writers_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database):
        pass
    with DatabaseOwnership(database):
        assert main(["--database", str(database), "health"]) == 0
        assert capsys.readouterr().out.strip() == "[]"
        assert main(["--database", str(database), "price", "list"]) == 0
        capsys.readouterr()
        assert (
            main(
                [
                    "--database",
                    str(database),
                    "price",
                    "approve",
                    "--feed-id",
                    "feed",
                    "--fingerprint",
                    "0" * 64,
                    "--reason",
                    "test",
                ],
            )
            == 2
        )
        assert "ownership unavailable" in capsys.readouterr().out
        assert (
            main(
                [
                    "--database",
                    str(database),
                    "baseline",
                    "approve",
                    "--feed-id",
                    "feed",
                    "--fingerprint",
                    "0" * 64,
                    "--reason",
                    "test",
                ],
            )
            == 2
        )
        assert "ownership unavailable" in capsys.readouterr().out


def test_ownership_requires_same_database_and_thread_binding_unchanged(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as owner, DeliveryStore(database) as store:
        with pytest.raises(DatabaseOwnershipError):
            owner.require(tmp_path / "other.db")
        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            pytest.raises(sqlite3.ProgrammingError, match="same thread"),
        ):
            executor.submit(store.load_price_snapshots, "feed").result(timeout=10)
