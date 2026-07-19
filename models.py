from dataclasses import dataclass
from typing import NewType

EntryId = NewType("EntryId", str)
FeedId = NewType("FeedId", str)


@dataclass(frozen=True, slots=True)
class EntryData:
    title: str
    link: str
    description: str
    author: str
    timestamp: str | None
