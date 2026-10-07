from datetime import date, timedelta

import pytest

from rss2discord.transports.listing_dates import SKOPJE, localize_skopje
from rss2discord.transports.pazar3_parser import SKOPJE as PAZAR3_SKOPJE
from rss2discord.transports.reklama5 import SKOPJE as REKLAMA5_SKOPJE


def test_listing_parsers_share_the_public_skopje_zone_object() -> None:
    assert PAZAR3_SKOPJE is SKOPJE
    assert REKLAMA5_SKOPJE is SKOPJE


def test_localize_skopje_rejects_nonexistent_spring_time() -> None:
    assert localize_skopje(date(2026, 3, 29), 2, 30) is None


def test_localize_skopje_uses_fold_zero_for_ambiguous_time() -> None:
    localized = localize_skopje(date(2026, 10, 25), 2, 30)

    assert localized is not None
    assert localized.fold == 0
    assert localized.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize(("hour", "minute"), [(24, 0), (2, 60)])
def test_localize_skopje_leaves_invalid_components_for_callers(
    hour: int,
    minute: int,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"hour must be in 0\.\.23|minute must be in 0\.\.59",
    ):
        localize_skopje(date(2026, 8, 1), hour, minute)
