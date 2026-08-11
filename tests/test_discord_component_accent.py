from dataclasses import replace

import pytest

from rss2discord.discord.components import (
    DEFAULT_ACCENT_COLOR,
    PRICE_DECREASE_ACCENT_COLOR,
    PRICE_INCREASE_ACCENT_COLOR,
)
from rss2discord.models import PriceDirection
from tests.discord_components_helpers import get_container, make_message


def _accent(*, embed_color: int | None, direction: PriceDirection | None) -> int:
    message = make_message(embed_color=embed_color)
    message = replace(
        message,
        entry=replace(message.entry, price_direction=direction),
    )
    accent = get_container(message)["accent_color"]
    assert isinstance(accent, int)
    return accent


def test_entry_without_direction_uses_the_configured_feed_colour() -> None:
    assert _accent(embed_color=0x123456, direction=None) == 0x123456


def test_entry_without_direction_or_feed_colour_falls_back_to_the_default() -> None:
    assert _accent(embed_color=None, direction=None) == DEFAULT_ACCENT_COLOR


def test_black_feed_colour_is_honoured_rather_than_treated_as_unset() -> None:
    # Black is falsy but is a real choice, so it must survive the fallback.
    assert _accent(embed_color=0, direction=None) == 0


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (PriceDirection.DECREASE, PRICE_DECREASE_ACCENT_COLOR),
        (PriceDirection.INCREASE, PRICE_INCREASE_ACCENT_COLOR),
    ],
    ids=["decrease", "increase"],
)
def test_price_direction_paints_the_accent_bar(
    direction: PriceDirection,
    expected: int,
) -> None:
    assert _accent(embed_color=None, direction=direction) == expected


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (PriceDirection.DECREASE, PRICE_DECREASE_ACCENT_COLOR),
        (PriceDirection.INCREASE, PRICE_INCREASE_ACCENT_COLOR),
    ],
    ids=["decrease", "increase"],
)
def test_price_direction_outranks_the_configured_feed_colour(
    direction: PriceDirection,
    expected: int,
) -> None:
    # Which way the price moved matters more than per-feed branding.
    assert _accent(embed_color=0x123456, direction=direction) == expected


def test_direction_colours_are_distinguishable_from_each_other() -> None:
    assert (
        len(
            {
                PRICE_DECREASE_ACCENT_COLOR,
                PRICE_INCREASE_ACCENT_COLOR,
                DEFAULT_ACCENT_COLOR,
            },
        )
        == 3
    )
