"""Container healthcheck evidence using local SQLite only."""

import sqlite3
from pathlib import Path

import pytest

from rss2discord import healthcheck


def _create_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO items DEFAULT VALUES")


def test_healthcheck_accepts_valid_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "state.db"
    _create_database(database)
    monkeypatch.setenv("STATE_DB_PATH", str(database))

    assert healthcheck.main() == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


def test_healthcheck_rejects_missing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "missing.db"))

    assert healthcheck.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.count("\n") == 1
    assert "FileNotFoundError" in output.err


def test_healthcheck_rejects_invalid_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "state.db"
    database.write_bytes(b"not a sqlite database")
    monkeypatch.setenv("STATE_DB_PATH", str(database))

    assert healthcheck.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.count("\n") == 1
