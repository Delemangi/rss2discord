from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from rss2discord.discord.client import SleepCallback
from rss2discord.providers.pazar3.pacing import Pazar3RequestPacer
from rss2discord.providers.pazar3.scope import PAZAR3_LABEL, Pazar3PageRequest
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.listing_http import ListingHttpConfig, fetch_listing_page

MAX_PAZAR3_RESPONSE_BYTES: Final = 2_097_152
MAX_PAZAR3_ATTEMPT_BYTES: Final = 6_291_456
MAX_PAZAR3_REDIRECTS: Final = 10
MAX_PAZAR3_ATTEMPT_SECONDS: Final = 120.0
MAX_PAZAR3_CATALOG_ATTEMPT_BYTES: Final = 20 * 1024 * 1024
MAX_PAZAR3_CATALOG_ATTEMPT_SECONDS: Final = 300.0
PAZAR3_USER_AGENT: Final = "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"

PAZAR3_LISTING_CONFIG: Final = ListingHttpConfig(
    label=PAZAR3_LABEL,
    max_response_bytes=MAX_PAZAR3_RESPONSE_BYTES,
    user_agent=PAZAR3_USER_AGENT,
    challenge_cause="AccessChallenge",
)


@dataclass(slots=True)  # noqa: RUF100  # noqa: MUTABLE_OK
class Pazar3ScanBudget:
    """Track mutable byte, redirect, and deadline limits for one fetch attempt."""

    bytes_remaining: int
    redirects_remaining: int
    expires_at: float

    @classmethod
    def for_attempt(cls) -> Pazar3ScanBudget:
        return cls(
            MAX_PAZAR3_ATTEMPT_BYTES,
            MAX_PAZAR3_REDIRECTS,
            time.monotonic() + MAX_PAZAR3_ATTEMPT_SECONDS,
        )

    @classmethod
    def for_catalog(cls) -> Pazar3ScanBudget:
        return cls(
            MAX_PAZAR3_CATALOG_ATTEMPT_BYTES,
            MAX_PAZAR3_REDIRECTS,
            time.monotonic() + MAX_PAZAR3_CATALOG_ATTEMPT_SECONDS,
        )

    def request_timeout(self) -> float:
        remaining_seconds = self.expires_at - time.monotonic()
        if remaining_seconds <= 0:
            raise FeedFetchError(PAZAR3_LABEL, "ScanTimeLimitExceeded")
        return min(30.0, remaining_seconds)

    def consume_redirect(self) -> None:
        if self.redirects_remaining <= 0:
            raise FeedFetchError(PAZAR3_LABEL, "TooManyRedirects")
        self.redirects_remaining -= 1

    def consume_bytes(self, size: int) -> None:
        if time.monotonic() >= self.expires_at:
            raise FeedFetchError(PAZAR3_LABEL, "ScanTimeLimitExceeded")
        if size > self.bytes_remaining:
            raise FeedFetchError(PAZAR3_LABEL, "ScanByteLimitExceeded")
        self.bytes_remaining -= size


def fetch_pazar3_page(
    request: Pazar3PageRequest,
    budget: Pazar3ScanBudget,
    pacer: Pazar3RequestPacer,
    sleep: SleepCallback,
    is_shutdown_requested: Callable[[], bool],
) -> bytes:
    """Fetch one trusted search page within a caller-owned attempt budget."""
    return fetch_listing_page(
        url=request.url,
        budget=budget,
        config=PAZAR3_LISTING_CONFIG,
        accepts_redirect=lambda target: request.scope.accepts_redirect(request, target),
        before_request=lambda: pacer.wait(
            sleep,
            is_shutdown_requested,
            budget.expires_at,
        ),
    )
