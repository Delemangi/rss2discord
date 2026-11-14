"""Pre-update backup lifecycle evidence using local SQLite only."""

import sqlite3
from pathlib import Path

import pytest

from rss2discord import backup


def _create_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        connection.executemany(
            "INSERT INTO items (name) VALUES (?)",
            (("one",), ("two",)),
        )


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    backup_dir: Path,
    *,
    retention: int = 10,
) -> None:
    monkeypatch.setenv("STATE_DB_PATH", str(source))
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("BACKUP_RETENTION", str(retention))


def test_valid_database_creates_verified_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "state.db"
    backup_dir = tmp_path / "backups"
    _create_database(source)
    _configure(monkeypatch, source, backup_dir)

    assert backup.main() == 0

    output = capsys.readouterr()
    backups = list(backup_dir.glob("state-pre-update-*.db"))
    assert len(backups) == 1
    assert output.out.strip() == str(backups[0])
    assert output.err == ""
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("SELECT name FROM items ORDER BY id").fetchall() == [
            ("one",),
            ("two",),
        ]


def test_retention_keeps_newest_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "state.db"
    backup_dir = tmp_path / "backups"
    _create_database(source)
    backup_dir.mkdir()
    for index in range(5):
        (backup_dir / f"state-pre-update-20000101T00000{index}.000000Z.db").write_bytes(
            b"old",
        )
    _configure(monkeypatch, source, backup_dir, retention=2)

    assert backup.main() == 0

    names = sorted(path.name for path in backup_dir.glob("state-pre-update-*.db"))
    assert len(names) == 2
    assert names[0] == "state-pre-update-20000101T000004.000000Z.db"
    assert names[1].startswith("state-pre-update-2")


def test_missing_source_returns_tempfail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "missing.db"
    backup_dir = tmp_path / "backups"
    _configure(monkeypatch, source, backup_dir)

    assert backup.main() == backup.EX_TEMPFAIL

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.count("\n") == 1
    assert "FileNotFoundError" in output.err
    assert list(backup_dir.glob("*")) == []


def test_failed_integrity_removes_temp_and_returns_tempfail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "state.db"
    backup_dir = tmp_path / "backups"
    _create_database(source)
    _configure(monkeypatch, source, backup_dir)
    monkeypatch.setattr(backup, "_integrity_ok", lambda _connection: False)

    assert backup.main() == backup.EX_TEMPFAIL

    assert capsys.readouterr().err.strip().endswith("backup integrity check failed")
    assert list(backup_dir.glob("*.tmp")) == []
    assert list(backup_dir.glob("state-pre-update-*.db")) == []


def test_backup_api_failure_removes_temp_and_returns_tempfail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "state.db"
    backup_dir = tmp_path / "backups"
    _create_database(source)
    _configure(monkeypatch, source, backup_dir)

    def fail(_source: sqlite3.Connection, _destination: sqlite3.Connection) -> None:
        raise RuntimeError("fixture backup failure")

    monkeypatch.setattr(backup, "_backup_database", fail)

    assert backup.main() == backup.EX_TEMPFAIL
    assert list(backup_dir.glob("*.tmp")) == []
    assert list(backup_dir.glob("state-pre-update-*.db")) == []


def test_invalid_retention_returns_tempfail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "state.db"
    _create_database(source)
    _configure(monkeypatch, source, tmp_path / "backups", retention=0)

    assert backup.main() == backup.EX_TEMPFAIL
