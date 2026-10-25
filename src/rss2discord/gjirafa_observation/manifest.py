"""Bounded versioned manifest input."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PureWindowsPath
from typing import Any

from rss2discord.gjirafa_observation_models import ObservationItem

MAX_BYTES = 8 * 1024 * 1024


def _check_local_path(path: Path, root: Path) -> None:
    """Reject remote/ambiguous Windows syntax before filesystem resolution."""
    windows = PureWindowsPath(str(path))
    root_drive = PureWindowsPath(str(root)).drive
    if str(path).startswith(("\\\\", "//")) or windows.drive.startswith("\\\\"):
        raise ValueError("manifest must be a local file inside the input root")
    if windows.drive and (
        not windows.root or windows.drive.casefold() != root_drive.casefold()
    ):
        raise ValueError("manifest drive is outside the input root")
    if str(path).startswith("\\") and not windows.drive:
        raise ValueError("manifest path must not be drive-relative")
    if any(":" in part for part in windows.parts[bool(windows.drive) :]):
        raise ValueError("invalid manifest path")


def _read_manifest(path: Path, input_root: Path) -> bytes:
    _check_local_path(path, input_root)
    try:
        root = input_root.resolve(strict=True)
        candidate = path if path.is_absolute() else root / path
        if candidate.suffix.lower() != ".json" or candidate.is_symlink():
            raise ValueError("manifest must be a regular JSON file")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("manifest is outside the input root")
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("manifest must be a regular JSON file within size limit")
        with resolved.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_BYTES:
                raise ValueError(
                    "manifest must be a regular JSON file within size limit",
                )
            raw = stream.read(MAX_BYTES + 1)
    except OSError:
        raise ValueError("manifest file unavailable") from None
    if len(raw) > MAX_BYTES:
        raise ValueError("manifest exceeds size limit")
    return raw


def _entries(raw: bytes) -> list[Any]:
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("invalid manifest JSON") from None
    if not isinstance(data, dict) or set(data) != {"version", "items"}:
        raise ValueError("invalid manifest envelope")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("unsupported manifest version")
    if not isinstance(data["items"], list) or not data["items"]:
        raise ValueError("manifest must contain items")
    return data["items"]


def _item(entry: Any) -> ObservationItem:  # noqa: ANN401
    from rss2discord.providers.gjirafa50.observation import (  # noqa: PLC0415
        validate_observation_item,
    )

    if not isinstance(entry, dict) or set(entry) != {
        "feed_id",
        "host",
        "product_id",
        "url",
    }:
        raise ValueError("invalid manifest item")
    if any(
        not isinstance(entry[key], str) or not entry[key]
        for key in (
            "feed_id",
            "host",
            "product_id",
        )
    ):
        raise ValueError("manifest identifiers must be strings")
    if not re.fullmatch(r"[1-9]\d*", entry["product_id"], flags=re.ASCII):
        raise ValueError("product ID must be a positive integer string")
    if entry["url"] is not None and not isinstance(entry["url"], str):
        raise ValueError("invalid URL")
    item = ObservationItem(**entry)
    validate_observation_item(item)
    return item


def load_manifest(path: Path, *, input_root: Path) -> tuple[list[ObservationItem], str]:
    entries = _entries(_read_manifest(path, input_root))
    items: list[ObservationItem] = []
    seen: set[tuple[str, str]] = set()
    hosts: dict[str, str] = {}
    for entry in entries:
        item = _item(entry)
        key = (item.feed_id, item.product_id)
        if key in seen or hosts.get(item.feed_id, item.host) != item.host:
            raise ValueError("duplicate item or mixed feed hosts")
        seen.add(key)
        hosts[item.feed_id] = item.host
        items.append(item)
    items.sort(key=lambda item: (item.feed_id, item.product_id))
    canonical = json.dumps(
        [[i.feed_id, i.host, i.product_id, i.url] for i in items],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return items, hashlib.sha256(canonical.encode()).hexdigest()
