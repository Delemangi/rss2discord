"""Local administration; only reconciliation plan/apply fetch source catalogs."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from rss2discord.configuration import load_config
from rss2discord.database_ownership import DatabaseOwnership, DatabaseOwnershipError
from rss2discord.delivery_store import DeliveryStore
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.reconciliation import apply_plan, create_plan
from rss2discord.reconciliation_models import MAX_PLAN_BYTES, ReconciliationPlan
from rss2discord.recovery_models import BaselineCandidateSummary, PriceBatch
from rss2discord.retries import FeedFetchInterruptedError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rss2discord-admin")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("STATE_DB_PATH", "data/state.db")),
        help="local SQLite state database (defaults to STATE_DB_PATH)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    price = commands.add_parser("price")
    price_commands = price.add_subparsers(dest="price_command", required=True)
    price_list = price_commands.add_parser("list")
    price_list.add_argument("--feed-id")
    price_list.add_argument("--all", action="store_true", dest="include_terminal")
    price_show = price_commands.add_parser("show")
    price_show.add_argument("batch_id", type=int)
    price_show.add_argument("--sample-limit", type=_sample_limit, default=5)
    price_show.add_argument("--offset", type=_offset, default=0)
    price_approve = price_commands.add_parser("approve")
    price_approve.add_argument("--feed-id", required=True)
    price_approve.add_argument("--fingerprint", required=True)
    price_approve.add_argument("--reason", required=True)
    price_revoke = price_commands.add_parser("revoke")
    price_revoke.add_argument("batch_id", type=int, nargs="?")
    price_revoke.add_argument("--fingerprint", required=True)
    price_revoke.add_argument("--feed-id")
    price_revoke.add_argument("--reason", required=True)

    baseline = commands.add_parser("baseline")
    baseline_commands = baseline.add_subparsers(dest="baseline_command", required=True)
    baseline_list = baseline_commands.add_parser("list")
    baseline_list.add_argument("--feed-id")
    baseline_show = baseline_commands.add_parser("show")
    baseline_show.add_argument("feed_id")
    baseline_show.add_argument("--sample-limit", type=_sample_limit, default=5)
    baseline_show.add_argument("--offset", type=_offset, default=0)
    baseline_approve = baseline_commands.add_parser("approve")
    baseline_approve.add_argument("--feed-id", required=True)
    baseline_approve.add_argument("--fingerprint", required=True)
    baseline_approve.add_argument("--reason", required=True)

    health = commands.add_parser("health")
    health.add_argument("health_command", nargs="?", choices=("list",), default="list")
    health.add_argument("--feed-id")

    reconcile = commands.add_parser("reconcile")
    reconciliation_commands = reconcile.add_subparsers(
        dest="reconciliation_command",
        required=True,
    )
    for name in ("plan", "apply"):
        command = reconciliation_commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--feed-id", required=True)
        command.add_argument(
            "--writers-stopped",
            action="store_true",
            required=True,
            help="confirm ALL services/old binaries writing this database were manually stopped",
        )
        if name == "plan":
            command.add_argument("--batch-id", type=int, required=True)
            command.add_argument("--batch-fingerprint", required=True)
            command.add_argument(
                "--reason",
                required=True,
                help="identify the actual operator and review ticket/reference; do not include credentials",
            )
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--plan", type=Path, required=True)
            command.add_argument("--fingerprint", required=True)
    review = reconciliation_commands.add_parser("review")
    review.add_argument("--plan", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)
    holds = reconciliation_commands.add_parser("holds")
    holds.add_argument("--feed-id")
    receipt = reconciliation_commands.add_parser("receipt")
    receipt.add_argument("--fingerprint", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "reconcile":
        return _reconciliation_command(args)
    writes = (
        args.command == "price" and args.price_command in {"approve", "revoke"}
    ) or (args.command == "baseline" and args.baseline_command == "approve")
    try:
        with (
            DatabaseOwnership(args.database) if writes else nullcontext(),
            DeliveryStore(args.database, read_only=not writes) as store,
        ):
            if args.command == "price":
                return _price_command(store, args)
            if args.command == "baseline":
                return _baseline_command(store, args)
            if args.command == "health":
                _print_json(
                    [asdict(record) for record in store.list_health(args.feed_id)],
                )
                return 0
    except DatabaseOwnershipError as error:
        return _error(str(error))
    return 2


def _read_plan(path: Path) -> ReconciliationPlan:
    with path.open("rb") as handle:
        data = handle.read(MAX_PLAN_BYTES + 1)
    if len(data) > MAX_PLAN_BYTES:
        raise ValueError("reconciliation artifact exceeds size limit")
    return ReconciliationPlan.model_validate_json(data)


def _write_plan(path: Path, plan: ReconciliationPlan) -> None:
    data = plan.model_dump_json(indent=2)
    if len(data.encode("utf-8")) > MAX_PLAN_BYTES:
        raise ValueError("reconciliation artifact exceeds size limit")
    with path.open("x", encoding="utf-8") as handle:
        handle.write(data + "\n")


def _reconciliation_command(args: argparse.Namespace) -> int:
    try:
        action = args.reconciliation_command
        if action == "review":
            plan = _read_plan(args.plan).sealed()
            _write_plan(args.output, plan)
            _print_json({"reconciliation_fingerprint": plan.reconciliation_fingerprint})
            return 0
        if action in {"holds", "receipt"}:
            with DeliveryStore(args.database, read_only=True) as store:
                if action == "holds":
                    records = store.list_price_product_holds(args.feed_id)
                    _print_json({"count": len(records), "holds": records})
                else:
                    receipt = store.load_price_reconciliation(args.fingerprint)
                    if receipt is None:
                        return _error("reconciliation receipt not found")
                    _print_json(receipt)
            return 0
        config = load_config(args.config)
        feed = next((feed for feed in config.feeds if feed.id == args.feed_id), None)
        if feed is None:
            return _error("feed not found in configuration")
        with DatabaseOwnership(args.database) as ownership:
            if action == "plan":
                with DeliveryStore(args.database, read_only=True) as store:
                    plan = create_plan(
                        store,
                        feed,
                        batch_id=args.batch_id,
                        batch_fingerprint=args.batch_fingerprint,
                        reason=args.reason,
                    )
                _write_plan(args.output, plan)
                _print_json(
                    {
                        "artifact": str(args.output),
                        "review_required": sum(
                            item.disposition == "review" for item in plan.items
                        ),
                    },
                )
            else:
                plan = _read_plan(args.plan)
                # Avoid even additive schema writes until evidence passes; the
                # reconciliation schema is created inside its atomic transaction.
                if not args.database.is_file():
                    return _error("existing database required")
                with DeliveryStore(args.database, initialize=False) as store:
                    _print_json(
                        apply_plan(
                            store,
                            feed,
                            plan,
                            ownership,
                            fingerprint=args.fingerprint,
                        ),
                    )
    except ValidationError:
        return _error("invalid reconciliation artifact or configuration")
    except yaml.YAMLError:
        return _error("invalid YAML reconciliation configuration")
    except (FeedFetchError, FeedFetchInterruptedError) as error:
        return _error(
            "reconciliation catalog collection failed (" + type(error).__name__ + ")",
        )
    except (DatabaseOwnershipError, ValueError, OSError, sqlite3.Error) as error:
        return _error("reconciliation refused: " + str(error))
    else:
        return 0


def _price_command(store: DeliveryStore, args: argparse.Namespace) -> int:
    if args.price_command == "list":
        summaries = store.list_price_change_batches(
            args.feed_id,
            include_terminal=args.include_terminal,
        )
        _print_json([asdict(summary) for summary in summaries])
        return 0
    if args.price_command == "show":
        batch = store.load_price_batch(args.batch_id)
        if batch is None:
            return _error("price batch not found")
        _print_json(_bounded_batch(batch, args.sample_limit, args.offset))
        return 0
    if args.price_command == "approve":
        batch = store.approve_price_change_batch(
            feed_id=args.feed_id,
            fingerprint=args.fingerprint,
            reason=args.reason,
        )
        _print_json(asdict(batch.summary))
        return 0
    if args.batch_id is None and (args.feed_id is None or args.fingerprint is None):
        return _error("revoke requires batch_id or feed-id plus fingerprint")
    batch = store.revoke_price_change_batch(
        args.batch_id,
        args.fingerprint,
        args.reason,
        feed_id=args.feed_id,
    )
    _print_json(asdict(batch.summary))
    return 0


def _baseline_command(store: DeliveryStore, args: argparse.Namespace) -> int:
    if args.baseline_command == "list":
        candidates = store.list_baseline_candidates(args.feed_id)
        _print_json([_bounded_baseline(candidate, 5) for candidate in candidates])
        return 0
    if args.baseline_command == "show":
        candidate = store.load_baseline_candidate(args.feed_id)
        if candidate is None:
            return _error("baseline candidate not found")
        _print_json(_bounded_baseline(candidate, args.sample_limit, args.offset))
        return 0
    candidate = store.approve_feed_baseline_candidate(
        feed_id=args.feed_id,
        fingerprint=args.fingerprint,
        reason=args.reason,
    )
    _print_json(_bounded_baseline(candidate, 5))
    return 0


def _bounded_batch(
    batch: PriceBatch,
    sample_limit: int,
    offset: int = 0,
) -> dict[str, Any]:
    data = asdict(batch.summary)
    data["items_sample"] = [
        {
            "product_id": item.product_id,
            "status": item.status,
            "attempt_count": item.attempt_count,
            "previous": {
                "amount": str(item.previous.amount),
                "currency": item.previous.currency,
                "formatted": item.previous.formatted,
            },
            "current": {
                "amount": str(item.current.amount),
                "currency": item.current.currency,
                "formatted": item.current.formatted,
            },
        }
        for item in batch.items[offset : offset + sample_limit]
    ]
    data.update(_page_metadata(offset, len(data["items_sample"]), len(batch.items)))
    return data


def _bounded_baseline(
    candidate: BaselineCandidateSummary,
    sample_limit: int,
    offset: int = 0,
) -> dict[str, Any]:
    data = asdict(candidate)
    data["entry_ids"] = list(candidate.entry_ids[offset : offset + sample_limit])
    data["entry_count"] = len(candidate.entry_ids)
    # Baseline records persist feed identity, not a provider name.
    data["provider"] = None
    data.update(
        _page_metadata(offset, len(data["entry_ids"]), len(candidate.entry_ids)),
    )
    return data


def _page_metadata(offset: int, returned: int, total: int) -> dict[str, int]:
    return {
        "offset": offset,
        "returned": returned,
        "remaining": max(0, total - offset - returned),
        "total": total,
    }


def _sample_limit(value: str) -> int:
    limit = int(value)
    if not 1 <= limit <= 1000:
        raise argparse.ArgumentTypeError("sample limit must be between 1 and 1000")
    return limit


def _offset(value: str) -> int:
    offset = int(value)
    if offset < 0:
        raise argparse.ArgumentTypeError("offset must be nonnegative")
    return offset


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True, default=str))


def _error(message: str) -> int:
    print(message)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
