"""Source adapter contract."""

from typing import Any, Protocol

from models import EntryData


class AdapterError(Exception):
    """Expected source-adapter enrichment failure."""


class SourceAdapter(Protocol):
    """Enrich normalized data using source-specific parsed entry fields."""

    def adapt(self, entry: Any, data: EntryData) -> EntryData:  # noqa: ANN401
        """Return source-specific data for a parsed feed entry."""
        ...
