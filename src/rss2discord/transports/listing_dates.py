import re
from datetime import UTC, date, datetime, time
from typing import Final
from zoneinfo import ZoneInfo

SKOPJE: Final = ZoneInfo("Europe/Skopje")
MONTH_ABBREVIATIONS: Final = {
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
TIME_PATTERN: Final = r"(?P<hour>\d{2}):(?P<minute>\d{2})"
TODAY_PATTERN: Final = re.compile(rf"Денес {TIME_PATTERN}", re.ASCII)
YESTERDAY_PATTERN: Final = re.compile(rf"Вчера {TIME_PATTERN}", re.ASCII)


def localize_skopje(wall_date: date, hour: int, minute: int) -> datetime | None:
    wall = datetime.combine(wall_date, time(hour, minute))
    localized = wall.replace(tzinfo=SKOPJE, fold=0)
    round_trip = localized.astimezone(UTC).astimezone(SKOPJE)
    if round_trip.replace(tzinfo=None) != wall or round_trip.fold != localized.fold:
        return None
    return localized
