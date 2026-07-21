from dataclasses import dataclass
from typing import NewType

EntryId = NewType("EntryId", str)
FeedId = NewType("FeedId", str)


@dataclass(frozen=True, slots=True)
class SourceMetric:
    label: str
    value: str


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
