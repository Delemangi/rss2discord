import re
from datetime import date, timedelta

import pytest

from rss2discord.providers.pazar3 import parser as pazar3_parser
from rss2discord.providers.reklama5 import parser as reklama5_parser
from rss2discord.transports.listing_activity import (
    ACTIVITY_TIME_PATTERN,
    SKOPJE,
    localize_wall_time,
)


def test_listing_parsers_share_the_public_skopje_zone_object() -> None:
    assert pazar3_parser.SKOPJE is SKOPJE
    assert reklama5_parser.SKOPJE is SKOPJE


def test_localize_wall_time_rejects_nonexistent_spring_time() -> None:
    match = re.fullmatch(ACTIVITY_TIME_PATTERN, "02:30")
    assert match is not None

    assert localize_wall_time(date(2026, 3, 29), match) is None


def test_localize_wall_time_uses_fold_zero_for_ambiguous_time() -> None:
    match = re.fullmatch(ACTIVITY_TIME_PATTERN, "02:30")
    assert match is not None

    localized = localize_wall_time(date(2026, 10, 25), match)

    assert localized is not None
    assert localized.fold == 0
    assert localized.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize("value", ["24:00", "02:60"])
def test_localize_wall_time_leaves_invalid_components_for_callers(
    value: str,
) -> None:
    match = re.fullmatch(ACTIVITY_TIME_PATTERN, value)
    assert match is not None

    selected_date = date(2026, 8, 1)
    with pytest.raises(
        ValueError,
        match=r"hour must be in 0\.\.23|minute must be in 0\.\.59",
    ):
        localize_wall_time(selected_date, match)
