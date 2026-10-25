"""Synthetic-only journal acceptance checks."""

import json
import os
import sqlite3
import stat
import time
from collections.abc import Iterable
from contextlib import closing
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from rss2discord.database_ownership import DatabaseOwnership, DatabaseOwnershipError
from rss2discord.gjirafa_observation import store
from rss2discord.gjirafa_observation.manifest import MAX_BYTES, load_manifest
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
    items, fingerprint = load_manifest(path, input_root=tmp_path)
    assert all(i.url is None for i in items)
    path.write_text(json.dumps({"version": 1, "items": list(reversed(entries))}))
    assert load_manifest(path, input_root=tmp_path)[1] == fingerprint
    path.write_text(json.dumps({"version": 2, "items": entries}))
    with pytest.raises(ValueError, match="version"):
        load_manifest(path, input_root=tmp_path)
    path.write_text(json.dumps({"version": 1, "items": [entries[0], entries[0]]}))
    with pytest.raises(ValueError, match="duplicate"):
        load_manifest(path, input_root=tmp_path)


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


def test_fatal_outcome_commits_then_stops(tmp_path: Path) -> None:
    path, source = setup(tmp_path)
    fake = Fake()
    fake.error = ObservationFetchError("physical budget exhausted", fatal=True)
    assert run(path, source, fake) == 1
    assert fake.calls == ["1"]
    assert report(path)["failed"] == 1
    assert report(path)["unchecked"] == 2
    fake.error = None
    run(path, source, fake, limit=1)
    assert fake.calls == ["1", "2"]


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


def test_manifest_local_relative_and_absolute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rss2discord.providers.gjirafa50.observation as transport  # noqa: PLC0415

    def validate(item: ObservationItem) -> None:
        assert item.host == "example.invalid"

    monkeypatch.setattr(transport, "validate_observation_item", validate)
    path = tmp_path / "scope.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "feed_id": "synthetic",
                        "host": "example.invalid",
                        "product_id": "1",
                        "url": None,
                    },
                ],
            },
        ),
    )
    assert load_manifest(Path("scope.json"), input_root=tmp_path) == load_manifest(
        path,
        input_root=tmp_path,
    )


@pytest.mark.parametrize(
    "candidate",
    [
        "../outside.json",
        "Z:/outside.json",
        "Z:relative.json",
        r"\\server\share\scope.json",
        "//server/share/scope.json",
    ],
)
def test_manifest_path_escape_never_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate: str,
) -> None:
    def forbidden_open(self: Path, *args: object, **kwargs: object) -> None:
        pytest.fail("rejected manifest path opened")

    monkeypatch.setattr(Path, "open", forbidden_open)
    path = Path(candidate)
    with pytest.raises(ValueError, match="manifest"):
        load_manifest(path, input_root=tmp_path)


@pytest.mark.parametrize("stage", ["schema", "metadata", "items", "publish", "fsync"])
@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_initialize_interruption_leaves_final_absent_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error: type[BaseException],
) -> None:
    path = tmp_path / store.FILENAME
    items = [
        ObservationItem("synthetic", "example.invalid", str(i), None) for i in (1, 2, 3)
    ]

    class InterruptedConnection(sqlite3.Connection):
        def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
            cursor = super().executescript(sql_script)
            if stage == "schema":
                raise error("injected initialization interruption")
            return cursor

        def executemany(
            self,
            sql: str,
            parameters: Iterable[Any],
            /,
        ) -> sqlite3.Cursor:
            if stage == "items" and "INSERT INTO items" in sql:
                super().executemany(sql, list(parameters)[:1])
                raise error("injected initialization interruption")
            cursor = super().executemany(sql, parameters)
            if stage == "metadata" and "INSERT INTO metadata" in sql:
                raise error("injected initialization interruption")
            return cursor

    def interrupt(*args: object, **kwargs: object) -> None:
        raise error("injected initialization interruption")

    with monkeypatch.context() as patch:
        patch.setattr(
            store.sqlite3,
            "connect",
            partial(sqlite3.connect, factory=InterruptedConnection),
        )
        if stage == "publish":
            patch.setattr(store.os, "link", interrupt)
        if stage == "fsync":
            patch.setattr(store.os, "fsync", interrupt)
        with pytest.raises(error, match="injected initialization"):
            initialize(path, items, "synthetic-fingerprint")
    assert not path.exists()
    assert {entry.name for entry in tmp_path.iterdir()} == {path.name + ".writer.lock"}
    initialize(path, items, "synthetic-fingerprint")
    store.inspect(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 3
        assert (
            db.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()[
                0
            ]
            == "synthetic-fingerprint"
        )


def test_initialize_publish_never_clobbers_foreign_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / store.FILENAME
    link = os.link
    private_paths: list[Path] = []

    def competing_link(source: Path, destination: Path) -> None:
        private_paths.append(source)
        store.inspect(source)
        with closing(sqlite3.connect(source)) as db:
            assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 1
        destination.write_bytes(b"foreign synthetic state")
        link(source, destination)

    monkeypatch.setattr(store.os, "link", competing_link)
    items = [ObservationItem("synthetic", "example.invalid", "1", None)]
    with pytest.raises(FileExistsError):
        initialize(
            path,
            items,
            "synthetic",
        )
    assert path.read_bytes() == b"foreign synthetic state"
    assert all(not temporary.exists() for temporary in private_paths)


def test_initialize_ignores_orphan_private_temp(tmp_path: Path) -> None:
    path = tmp_path / store.FILENAME
    orphan = tmp_path / ("." + path.name + ".orphan.tmp")
    orphan.write_bytes(b"interrupted synthetic initialization")
    initialize(
        path,
        [ObservationItem("synthetic", "example.invalid", "1", None)],
        "synthetic",
    )
    store.inspect(path)
    assert orphan.read_bytes() == b"interrupted synthetic initialization"


def test_initialize_closes_connections_before_atomic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / store.FILENAME
    original_connect = sqlite3.connect
    original_link = os.link
    opened: list[sqlite3.Connection] = []
    closed: list[sqlite3.Connection] = []

    class TrackedConnection(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            closed.append(self)

    def connect(database: str | Path, *, uri: bool = False) -> sqlite3.Connection:
        db = original_connect(database, uri=uri, factory=TrackedConnection)
        opened.append(db)
        return db

    def publish(source: Path, destination: Path) -> None:
        assert opened
        assert opened == closed
        assert not destination.exists()
        with pytest.raises(DatabaseOwnershipError), DatabaseOwnership(destination):
            pytest.fail("publication released final-path writer ownership")
        original_link(source, destination)

    monkeypatch.setattr(store.sqlite3, "connect", connect)
    monkeypatch.setattr(store.os, "link", publish)
    initialize(
        path,
        [ObservationItem("synthetic", "example.invalid", "1", None)],
        "synthetic",
    )
    store.inspect(path)


def test_manifest_absolute_outside_never_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("private synthetic content")

    def forbidden_open(self: Path, *args: object, **kwargs: object) -> None:
        pytest.fail("outside manifest opened")

    monkeypatch.setattr(Path, "open", forbidden_open)
    with pytest.raises(ValueError, match="outside"):
        load_manifest(outside, input_root=root)


@pytest.mark.parametrize(
    "kind",
    ["directory", "non-json", "oversized", "symlink", "parent-symlink"],
)
def test_manifest_invalid_file_never_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    candidate = root / "scope.json"
    if kind == "directory":
        candidate.mkdir()
    elif kind == "non-json":
        candidate = root / "scope.txt"
        candidate.write_text("{}")
    elif kind == "oversized":
        with candidate.open("wb") as stream:
            stream.truncate(MAX_BYTES + 1)
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "scope.json").write_text("private synthetic content")
        try:
            if kind == "symlink":
                candidate.symlink_to(outside / "scope.json")
            else:
                (root / "linked").symlink_to(outside, target_is_directory=True)
                candidate = root / "linked" / "scope.json"
        except OSError:
            pytest.skip("symlink creation unavailable")

    def forbidden_open(self: Path, *args: object, **kwargs: object) -> None:
        pytest.fail("invalid manifest opened")

    monkeypatch.setattr(Path, "open", forbidden_open)
    with pytest.raises(ValueError, match="manifest"):
        load_manifest(candidate, input_root=root)


def test_manifest_open_descriptor_size_rechecked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "scope.json"
    path.write_text("{}")
    original = os.fstat

    def enlarged(descriptor: int) -> os.stat_result:
        fields = list(original(descriptor))
        fields[6] = MAX_BYTES + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", enlarged)
    with pytest.raises(ValueError, match="size limit"):
        load_manifest(path, input_root=tmp_path)


@pytest.mark.parametrize(
    "raw",
    [b'{"private synthetic content":', b"\xffprivate synthetic content"],
)
def test_manifest_parse_error_does_not_echo_content(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "scope.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match=r"^invalid manifest JSON$"):
        load_manifest(path, input_root=tmp_path)


@pytest.mark.parametrize(
    "candidate",
    ["Z:/scope.json", "Z:scope.json", r"\\server\share\scope.json"],
)
def test_manifest_remote_paths_rejected_before_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate: str,
) -> None:
    def forbidden_resolve(self: Path, *, strict: bool = False) -> Path:
        pytest.fail("remote or ambiguous path reached filesystem resolution")

    monkeypatch.setattr(Path, "resolve", forbidden_resolve)
    path = Path(candidate)
    with pytest.raises(ValueError, match="manifest"):
        load_manifest(path, input_root=tmp_path)
