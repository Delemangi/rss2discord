"""Synthetic-only journal acceptance checks."""

import json
import os
import sqlite3
import stat
import time
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.database_ownership import DatabaseOwnership, DatabaseOwnershipError
from rss2discord.gjirafa_observation.manifest import load_manifest
from rss2discord.gjirafa_observation.report import report
from rss2discord.gjirafa_observation.runner import run
from rss2discord.gjirafa_observation.store import initialize
from rss2discord.gjirafa_observation_models import (
    ObservationFetchError,
    ObservationItem,
    ScalarObservation,
)
from rss2discord.gjirafa_observe import main


class Fake:
    def __init__(self, amount: str = "1.0") -> None:
        self.amount = amount
        self.calls: list[str] = []
        self.error: ObservationFetchError | None = None
        self.crash = False
        self.source: Path | None = None

    def fetch(self, item: ObservationItem) -> ScalarObservation:
        self.calls.append(item.product_id)
        if self.crash:
            raise RuntimeError("simulated crash after HTTP")
        if self.source:
            with sqlite3.connect(self.source) as db:
                db.execute(
                    "UPDATE health_state SET blocked_until=?",
                    (int(time.time()) + 3600,),
                )
        if self.error:
            raise self.error
        return ScalarObservation(
            Decimal(self.amount),
            "EUR",
            item.url or "",
            int(time.time()),
        )


def setup(tmp_path: Path, count: int = 3) -> tuple[Path, Path]:
    path, source = tmp_path / "observe.sqlite3", tmp_path / "source.sqlite3"
    items = [
        ObservationItem(
            "synthetic",
            "example.invalid",
            str(i + 1),
            f"https://example.invalid/{i + 1}",
        )
        for i in range(count)
    ]
    initialize(path, items, "synthetic")
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE health_state(feed_id TEXT,job_kind TEXT,blocked_until INTEGER)",
        )
        db.execute("INSERT INTO health_state VALUES ('synthetic','ordinary',NULL)")
    return path, source


def test_epochs_cumulative_and_numeric(tmp_path: Path) -> None:
    path, source = setup(tmp_path, 101)
    for limit in (40, 40, 40):
        run(path, source, Fake(), limit=limit)
    assert report(path)["state"] == "manifest pass completed"
    assert report(path)["comparable"] == 0
    for limit in (40, 40, 40):
        run(path, source, Fake("2"), limit=limit)
        assert report(path)["reference_completed_epoch"] == 1
    assert report(path)["changed_distinct_ids"] == 101
    assert report(path)["review_warning"] is True
    run(path, source, Fake("2.00"), limit=100)
    assert report(path)["changed_distinct_ids"] == 0


def test_crash_never_skips_and_lock_excludes(tmp_path: Path) -> None:
    path, source = setup(tmp_path)
    fake = Fake()
    fake.crash = True
    with pytest.raises(RuntimeError, match="simulated"):
        run(path, source, fake)
    assert report(path)["unchecked"] == 3
    fake.crash = False
    run(path, source, fake, limit=1)
    assert fake.calls == ["1", "1"]
    with DatabaseOwnership(path), pytest.raises(DatabaseOwnershipError):
        run(path, source, fake)


def test_source_gate_and_persisted_block(tmp_path: Path) -> None:
    path, source = setup(tmp_path)
    fake = Fake()
    with pytest.raises(sqlite3.Error):
        run(path, tmp_path / "missing", fake)
    assert not fake.calls
    fake.error = ObservationFetchError("blocked", stop_host=True)
    run(path, source, fake, limit=1)
    run(path, source, fake)
    assert fake.calls == ["1"]
    assert report(path)["blocked"] == 1
    assert report(path)["cooldown"] == 2


def test_source_gate_refresh(tmp_path: Path) -> None:
    path, source = setup(tmp_path)
    fake = Fake()
    fake.source = source
    run(path, source, fake)
    assert fake.calls == ["1"]
    assert report(path)["cooldown"] == 2


def test_foreign_and_readonly_and_replacement(tmp_path: Path) -> None:
    path, _ = setup(tmp_path)
    before = path.read_bytes()
    report(path)
    assert path.read_bytes() == before
    initialize(path, [], "synthetic")
    with pytest.raises(ValueError, match="replacement"):
        initialize(path, [], "different")
    foreign = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(foreign) as db:
        db.execute("CREATE TABLE production(secret TEXT)")
    before = foreign.read_bytes()
    with pytest.raises(ValueError, match="not an observation"):
        initialize(foreign, [], "different")
    assert foreign.read_bytes() == before


def test_manifest_fingerprint_and_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rss2discord.providers.gjirafa50.observation as transport  # noqa: PLC0415

    def validate(item: ObservationItem) -> None:
        assert item.host == "example.invalid"

    monkeypatch.setattr(transport, "validate_observation_item", validate)
    path = tmp_path / "manifest.json"
    entries = [
        {
            "feed_id": "synthetic",
            "host": "example.invalid",
            "product_id": str(i),
            "url": None,
        }
        for i in (2, 1)
    ]
    path.write_text(json.dumps({"version": 1, "items": entries}))
    items, fingerprint = load_manifest(path)
    assert all(i.url is None for i in items)
    path.write_text(json.dumps({"version": 1, "items": list(reversed(entries))}))
    assert load_manifest(path)[1] == fingerprint
    path.write_text(json.dumps({"version": 2, "items": entries}))
    with pytest.raises(ValueError, match="version"):
        load_manifest(path)
    path.write_text(json.dumps({"version": 1, "items": [entries[0], entries[0]]}))
    with pytest.raises(ValueError, match="duplicate"):
        load_manifest(path)


def test_unknown_and_incomparable(tmp_path: Path) -> None:
    path, source = setup(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE items SET url=NULL WHERE ordinal=0")
    fake = Fake()
    fake.error = ObservationFetchError("unvalidated")
    run(path, source, fake)
    assert report(path)["unknown"] == 1
    assert report(path)["failed"] == 2
    run(path, source, Fake())
    assert report(path)["comparable"] == 0
    assert report(path)["incomparable"] == 3


def test_scalar_rejection_blocks_item_without_stopping_host(tmp_path: Path) -> None:
    path, source = setup(tmp_path)
    fake = Fake()
    fake.error = ObservationFetchError("ScalarContractRejected")
    run(path, source, fake)
    assert fake.calls == ["1", "2", "3"]
    assert report(path)["blocked"] == 3
    assert report(path)["failed"] == 0
    assert report(path)["cooldown"] == 0


def test_atomic_observation_cursor_commit(tmp_path: Path) -> None:
    path, source = setup(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER fault BEFORE UPDATE ON epochs BEGIN SELECT RAISE(ABORT,'fault'); END",
        )
    fake = Fake()
    with pytest.raises(sqlite3.IntegrityError, match="fault"):
        run(path, source, fake)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        assert db.execute("SELECT cursor FROM epochs").fetchone()[0] == 0
        db.execute("DROP TRIGGER fault")
    run(path, source, fake, limit=1)
    assert fake.calls == ["1", "1"]


def test_cli_global_state_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, _ = setup(tmp_path)
    destination = tmp_path / "gjirafa-observation.sqlite3"
    path.rename(destination)
    before = destination.read_bytes()
    for command in ("status", "report"):
        assert main(["--state-dir", str(tmp_path), command]) == 0
        assert json.loads(capsys.readouterr().out)["known"] == 3
    assert destination.read_bytes() == before


@pytest.mark.parametrize(
    ("count", "shared_ids", "warning"),
    [(101, True, True), (51, False, False)],
)
def test_feed_scoped_changes(
    tmp_path: Path,
    count: int,
    *,
    shared_ids: bool,
    warning: bool,
) -> None:
    path = tmp_path / "observe.sqlite3"
    items = [
        ObservationItem(feed, "example.invalid", str(i + 1 + offset), None)
        for feed, offset in (
            ("synthetic-a", 0),
            ("synthetic-b", 0 if shared_ids else count),
        )
        for i in range(count)
    ]
    initialize(path, items, "synthetic")
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO epochs VALUES (?,?,?,1)",
            [(1, None, len(items)), (2, 1, len(items))],
        )
        db.executemany(
            "INSERT INTO observations VALUES (?,?, 'validated',1,?,'EUR',NULL,NULL)",
            [
                (epoch, ordinal, amount)
                for epoch, amount in ((1, "1"), (2, "2"))
                for ordinal in range(len(items))
            ],
        )
    result = report(path)
    assert result["changed_distinct_ids"] == count * 2
    assert result["changed_per_feed"] == {"synthetic-a": count, "synthetic-b": count}
    assert result["review_warning"] is warning


def test_source_gate_queries_unique_feeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rss2discord.gjirafa_observation import runner  # noqa: PLC0415

    path, source = setup(tmp_path, 50)
    queries: list[str] = []
    original = runner.readonly

    def traced(database: Path) -> sqlite3.Connection:
        db = original(database)
        db.set_trace_callback(queries.append)
        return db

    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO health_state VALUES ('synthetic-b','price',NULL)")
    items = [
        ObservationItem(feed, "example.invalid", str(i + 1), None)
        for feed in ("synthetic", "synthetic-b")
        for i in range(50)
    ]
    monkeypatch.setattr(runner, "readonly", traced)
    runner.source_gate(source, path, items)
    selects = [query for query in queries if query.startswith("SELECT blocked_until")]
    assert len(selects) == 2


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permission bits are not Windows ACLs",
)
def test_private_database_permissions(tmp_path: Path) -> None:
    path, _ = setup(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_report_read_transaction_while_writer_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib  # noqa: PLC0415

    reporting = importlib.import_module("rss2discord.gjirafa_observation.report")
    path, source = setup(tmp_path)
    run(path, source, Fake(), limit=1)
    before = path.read_bytes()
    queries: list[str] = []
    original = reporting.readonly

    def traced(database: Path) -> sqlite3.Connection:
        db = original(database)
        db.set_trace_callback(queries.append)
        return db

    monkeypatch.setattr(reporting, "readonly", traced)
    with DatabaseOwnership(path):
        result = reporting.report(path)
    assert queries[0] == "BEGIN"
    assert all(query.lstrip().startswith("SELECT") for query in queries[1:])
    assert result["unchecked"] == 2
    assert result["incomparable"] == 1
    assert path.read_bytes() == before


def test_report_manifest_and_feed_coverage(tmp_path: Path) -> None:
    path, source = setup(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE items SET url=NULL WHERE ordinal IN (0,1)")
        db.executemany(
            "INSERT INTO items VALUES (?,?,?,?,?)",
            [
                (3, "synthetic-b", "example.invalid", "1", "https://example.invalid/1"),
                (4, "synthetic-b", "example.invalid", "2", None),
            ],
        )
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO health_state VALUES ('synthetic-b','ordinary',NULL)")
    initial = report(path)
    assert initial["manifest_fingerprint"] == "synthetic"
    first = initial["coverage_per_feed"]["synthetic"]
    second = initial["coverage_per_feed"]["synthetic-b"]
    assert (
        first["known"],
        first["mapped"],
        first["URL_unknown"],
        first["unchecked"],
    ) == (3, 1, 2, 3)
    assert (
        second["known"],
        second["mapped"],
        second["URL_unknown"],
        second["unchecked"],
    ) == (2, 1, 1, 2)
    assert first["unknown"] == second["validated"] == 0
    run(path, source, Fake(), limit=4)
    partial = report(path)
    assert partial["manifest_fingerprint"] == "synthetic"
    first = partial["coverage_per_feed"]["synthetic"]
    second = partial["coverage_per_feed"]["synthetic-b"]
    assert (first["unknown"], first["validated"], first["unchecked"]) == (2, 1, 0)
    assert (second["unknown"], second["validated"], second["unchecked"]) == (0, 1, 1)
    assert partial["unchecked"] == 1
