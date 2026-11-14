"""Read-only local inspection for health and price notifications."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rss2discord-admin")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("STATE_DB_PATH", "data/state.db")),
        help="local SQLite state database (defaults to STATE_DB_PATH)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    health = commands.add_parser("health")
    health.add_argument("health_command", nargs="?", choices=("list",), default="list")
    health.add_argument("--feed-id")

    notifications = commands.add_parser("notifications")
    notifications.add_argument(
        "notification_command",
        nargs="?",
        choices=("list",),
        default="list",
    )
    notifications.add_argument("--feed-id")
    notifications.add_argument("--status", choices=("pending", "sent"))
    notifications.add_argument("--limit", type=int, default=100)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with DeliveryStore(args.database, read_only=True) as store:
            if args.command == "health":
                _print_json(
                    [asdict(record) for record in store.list_health(args.feed_id)],
                )
                return 0
            if args.command == "notifications":
                _print_json(
                    list(
                        store.list_price_notifications(
                            args.feed_id,
                            args.status,
                            args.limit,
                        ),
                    ),
                )
                return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        return _error(str(error))
    return 2


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True, default=str))


def _error(message: str) -> int:
    print(message)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
