"""Shared Macedonian listing-activity vocabulary and Skopje wall-time handling."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from typing import Final
from zoneinfo import ZoneInfo

SKOPJE: Final = ZoneInfo("Europe/Skopje")
MACEDONIAN_MONTHS: Final = {
    "јан": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "мај": 5,
    "јун": 6,
    "јул": 7,
    "авг": 8,
    "сеп": 9,
    "окт": 10,
    "ное": 11,
    "дек": 12,
}
ACTIVITY_TIME_PATTERN: Final = r"(?P<hour>\d{2}):(?P<minute>\d{2})"
TODAY_PATTERN: Final = re.compile(rf"Денес {ACTIVITY_TIME_PATTERN}", re.ASCII)
YESTERDAY_PATTERN: Final = re.compile(rf"Вчера {ACTIVITY_TIME_PATTERN}", re.ASCII)


def localize_wall_time(wall_date: date, match: re.Match[str]) -> datetime | None:
    wall = datetime.combine(
        wall_date,
        time(int(match.group("hour")), int(match.group("minute"))),
    )
    localized = wall.replace(tzinfo=SKOPJE, fold=0)
    round_trip = localized.astimezone(UTC).astimezone(SKOPJE)
    if round_trip.replace(tzinfo=None) != wall or round_trip.fold != localized.fold:
        return None
    return localized
