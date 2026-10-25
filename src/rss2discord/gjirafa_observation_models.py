"""Observation-only boundaries, deliberately independent of delivery models."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ObservationItem:
    feed_id: str
    host: str
    product_id: str
    url: str | None


@dataclass(frozen=True, slots=True)
class ScalarObservation:
    amount: Decimal
    currency: str
    canonical_url: str
    observed_at: int


class ObservationFetchError(Exception):
    """Safe failure metadata, never a missing-product or zero-price assertion."""

    def __init__(
        self,
        reason: str,
        *,
        blocked_until: int | None = None,
        stop_host: bool = False,
        fatal: bool = False,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.blocked_until = blocked_until
        self.stop_host = stop_host
        self.fatal = fatal


class ObservationFetcher(Protocol):
    def fetch(self, item: ObservationItem) -> ScalarObservation: ...
