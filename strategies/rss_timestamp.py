"""RSS and Atom timestamp normalization."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


def get_rss_timestamp(
    entry: Any,  # noqa: ANN401
    parse_timestamp: Callable[[Any], str | None],
) -> str | None:
    for field in ("published_parsed", "updated_parsed"):
        parsed_time = _direct_field(entry, field)
        if parsed_time is None:
            continue
        try:
            parsed_datetime = datetime(
                parsed_time.tm_year,
                parsed_time.tm_mon,
                parsed_time.tm_mday,
                parsed_time.tm_hour,
                parsed_time.tm_min,
                parsed_time.tm_sec,
                tzinfo=UTC,
            )
        except (AttributeError, ValueError):
            continue
        return parsed_datetime.isoformat()
    for field in ("published", "updated"):
        parsed_timestamp = parse_timestamp(_direct_field(entry, field))
        if parsed_timestamp is not None:
            return parsed_timestamp
    return None


def _direct_field(entry: Any, field: str) -> Any:  # noqa: ANN401
    if isinstance(entry, dict):
        return dict.get(entry, field)
    return entry.get(field)
