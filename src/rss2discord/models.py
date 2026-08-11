from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

EntryId = NewType("EntryId", str)
FeedId = NewType("FeedId", str)


class PriceDirection(StrEnum):
    """Which way a monitored price moved, when the mover is known.

    Left unset when the comparison is not a straight up/down, such as a change
    that crossed currencies.
    """

    DECREASE = "decrease"
    INCREASE = "increase"


@dataclass(frozen=True, slots=True)
class SourceMetric:
    label: str
    value: str
    # Marks the figure the headline metric replaced, such as the price a drop
    # alert moved away from. It is rendered struck through beside the headline
    # instead of being demoted into the supporting line.
    prior: bool = False


@dataclass(frozen=True, slots=True)
class EntryData:
    title: str
    link: str
    description: str
    author: str
    timestamp: str | None
    discussion_url: str | None = None
    image_url: str | None = None
    categories: tuple[str, ...] = ()
    source_metrics: tuple[SourceMetric, ...] = ()
    price_direction: PriceDirection | None = None
