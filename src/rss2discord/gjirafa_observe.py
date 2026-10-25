"""Explicit isolated observation-only command line."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from rss2discord.database_ownership import DatabaseOwnership, DatabaseOwnershipError
from rss2discord.gjirafa_observation.manifest import load_manifest
from rss2discord.gjirafa_observation.report import report
from rss2discord.gjirafa_observation.runner import run_owned, source_gate
from rss2discord.gjirafa_observation.store import (
    FILENAME,
    initialize,
    inspect,
    readonly,
)
from rss2discord.gjirafa_observation_models import ObservationItem


def main(argv: list[str] | None = None) -> int:
    input_root = Path.cwd().resolve()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "run", "status", "report"):
        command = commands.add_parser(name)
        if name == "init":
            command.add_argument("--manifest", required=True, type=Path)
        if name == "run":
            command.add_argument("--source-state", required=True, type=Path)
            command.add_argument("--limit", type=int, default=25, choices=range(1, 101))
    args = parser.parse_args(argv)
    path = args.state_dir / FILENAME
    try:
        if args.command == "init":
            items, fingerprint = load_manifest(args.manifest, input_root=input_root)
            initialize(path, items, fingerprint)
        elif args.command == "run":
            with DatabaseOwnership(path) as ownership:
                inspect(path)
                with closing(readonly(path)) as db:
                    items = [
                        ObservationItem(*row)
                        for row in db.execute(
                            "SELECT feed_id,host,product_id,url FROM items ORDER BY ordinal",
                        )
                    ]
                source_gate(args.source_state, path, items)
                from rss2discord.providers.gjirafa50.observation import (  # noqa: PLC0415
                    GjirafaObservationClient,
                )

                with GjirafaObservationClient() as client:
                    run_owned(
                        path,
                        args.source_state,
                        client,
                        ownership=ownership,
                        limit=args.limit,
                    )
        print(json.dumps(report(path), sort_keys=True))
    except (ValueError, OSError, sqlite3.Error, DatabaseOwnershipError) as error:
        parser.exit(1, f"Observation refused: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
