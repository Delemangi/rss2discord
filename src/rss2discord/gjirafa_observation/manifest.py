"""Bounded versioned manifest input."""

import hashlib
import json
import re
from pathlib import Path

from rss2discord.gjirafa_observation_models import ObservationItem

MAX_BYTES = 8 * 1024 * 1024


def load_manifest(path: Path) -> tuple[list[ObservationItem], str]:
    from rss2discord.providers.gjirafa50.observation import (  # noqa: PLC0415
        validate_observation_item,
    )

    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("manifest exceeds size limit")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"version", "items"}:
        raise ValueError("invalid manifest envelope")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("unsupported manifest version")
    if not isinstance(data["items"], list) or not data["items"]:
        raise ValueError("manifest must contain items")
    items: list[ObservationItem] = []
    seen: set[tuple[str, str]] = set()
    hosts: dict[str, str] = {}
    for entry in data["items"]:
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
        if not re.fullmatch(r"[1-9][0-9]*", entry["product_id"]):
            raise ValueError("product ID must be a positive integer string")
        if entry["url"] is not None and not isinstance(entry["url"], str):
            raise ValueError("invalid URL")
        item = ObservationItem(**entry)
        validate_observation_item(item)
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
